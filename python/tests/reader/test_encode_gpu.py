"""NVENC's half of the delivery encoder: the samples, the VUI, and device input.

Skipped without the GPU wheels or an encode-capable device. Everything here is a
property the CPU arm already has — the point is that the two agree, because a
delivery whose colour depends on which machine encoded it is the bug the
`full_range` argument exists to prevent.
"""

from __future__ import annotations

import av
import numpy as np
import pytest

cupy = pytest.importorskip("cupy", reason="NVENC tests need the gpu wheels")
pytest.importorskip("cvcuda", reason="NVENC device input needs an NVCV tensor")
pytest.importorskip("PyNvVideoCodec", reason="NVENC tests need PyNvVideoCodec")

from visio_schema.reader._encode import HevcEncoder, NvHevcEncoder  # noqa: E402

W, H = 192, 128
T0 = 1_700_000_000 * 1_000_000_000
DT = 33_000_000


@pytest.fixture(scope="module", autouse=True)
def nvenc():
    """An encode-capable device, or skip. Construction is the real probe.

    The probe session is ENDED before the tests run: a consumer card caps concurrent
    NVENC sessions, and holding one for the module's lifetime is a flake source.
    """
    try:
        probe = NvHevcEncoder(W, H, keyint=10)
    except Exception as e:  # no device, no driver, session cap
        pytest.skip(f"NVENC unavailable: {e}")
    probe.flush()


def _flat(value):
    return np.full((H, W, 3), value, np.uint8)


def _planes(encoder, frames) -> tuple[list[np.ndarray], object]:
    """Every decoded frame as its full YUV420 planes, plus the stream's own tags.

    Planes, not a median of Y: chroma is where the two backends could silently
    disagree (a U/V swap delivers red as blue and leaves every luma assertion green).
    """
    aus = []
    for i, f in enumerate(frames):
        aus += [au for _t, au in encoder.encode(f, T0 + i * DT)]
    aus += [au for _t, au in encoder.flush()]
    ctx = av.CodecContext.create("hevc", "r")
    out, tags = [], None
    for au in aus:
        for frame in ctx.decode(av.Packet(au)):
            out.append(frame.to_ndarray())
            got = (frame.format.name, int(frame.color_range), int(frame.colorspace))
            assert tags in (None, got), "the stream changed its own tags mid-way"
            tags = got
    return out, tags


def _luma(encoder, frames) -> tuple[list[int], object]:
    planes, tags = _planes(encoder, frames)
    return [int(np.median(p[:H])) for p in planes], tags


GREY = [_flat(0), _flat(128), _flat(255)]


def test_nvenc_full_range_samples_and_vui():
    """NVENC's own packed-RGB path cannot do this: the driver converts with a fixed
    BT.470BG limited matrix and writes `video_signal_type_present_flag = 0`. We feed
    it NV12 we converted ourselves and insert the VUI it will not."""
    luma, tags = _luma(NvHevcEncoder(W, H, keyint=10, full_range=True), GREY)
    assert luma == [0, 128, 255]
    assert tags == ("yuvj420p", 2, 1)  # full range, BT.709


def test_nvenc_limited_keeps_what_the_abgr_path_produced():
    """The default arm must not move: the reference video its callers already ship
    was BT.601 limited, which is what NVENC's packed-RGB conversion produced."""
    luma, tags = _luma(NvHevcEncoder(W, H, keyint=10), GREY)
    assert luma == [16, 126, 235]
    assert tags[0] == "yuv420p"


def test_nvenc_and_libx265_agree_on_the_whole_picture():
    """The cross-backend property, chroma included. Both arms build full-range BT.709
    deliberately and by different means — one through swscale, one through our own
    cupy kernel — so this is the test that they arrived at the same place."""
    red = np.zeros((H, W, 3), np.uint8)
    red[..., 0] = 255
    blue = np.zeros((H, W, 3), np.uint8)
    blue[..., 2] = 255
    frames = [*GREY, red, blue]
    gpu, gpu_tags = _planes(NvHevcEncoder(W, H, keyint=10, full_range=True), frames)
    cpu, cpu_tags = _planes(HevcEncoder(W, H, keyint=10, full_range=True), frames)
    assert gpu_tags == cpu_tags == ("yuvj420p", 2, 1)
    assert len(gpu) == len(cpu) == len(frames)
    for i, (g, c) in enumerate(zip(gpu, cpu, strict=True)):
        # Y AND both chroma planes; the slack is two independent codecs at the same
        # rate point on a flat frame, not a different colour.
        assert np.abs(g.astype(int) - c.astype(int)).max() <= 3, f"frame {i}"
    assert [int(np.median(p[:H])) for p in gpu] == [0, 128, 255, 54, 18]


def test_nvenc_takes_a_device_frame_without_a_host_round_trip():
    """The whole point of the NV12 path: a device-resident pipeline never copies
    the picture back to system memory to encode it."""
    enc = NvHevcEncoder(W, H, keyint=10, full_range=True)
    device = [cupy.asarray(f) for f in GREY]
    for f in device:
        assert hasattr(f, "__cuda_array_interface__")
    luma, tags = _luma(enc, device)
    assert luma == [0, 128, 255]
    assert tags == ("yuvj420p", 2, 1)


def test_parameter_sets_are_swapped_only_on_irap():
    """The patch is a known-prefix substitution, not a per-frame filter: NVENC emits
    the same parameter-set blob in front of every IRAP and nothing else."""
    enc = NvHevcEncoder(W, H, keyint=10, full_range=True)
    original, patched = enc._params
    assert patched != original and patched.startswith(b"\x00\x00\x00\x01")
    aus = []
    for i in range(12):
        aus += [au for _t, au in enc.encode(_flat(100), T0 + i * DT)]
    aus += [au for _t, au in enc.flush()]
    swapped = [au for au in aus if au.startswith(patched)]
    assert swapped, "no access unit carried the patched parameter sets"
    assert not any(au.startswith(original) for au in aus)


def test_two_encoders_never_deliver_each_others_pictures():
    """The pack's real shape: TWO sessions alternating, one per eye.

    Three ingredients, and dropping any one makes this pass on the broken code:
    full resolution and textured (a 192x128 flat frame encodes before the next submit
    can collide with it), two encoders alternating (one has nobody to steal from), and
    a FRESH device allocation per frame — that churn is what hands one encoder the
    block the other just freed, and it is what the real path does (NVDEC copies out
    into a new buffer, cvcuda.remap allocates its output). Pre-uploading the frames
    once makes the race disappear.

    The eye tag alone catches 100% of cross-encoder corruption (measured: it is whole
    -frame substitution, never a tear). The index tag is not redundant with it — it is
    the only thing here that can catch a SAME-encoder stale read, which is what
    ``_INFLIGHT`` guards.
    """
    w, h, n, distinct = 1920, 1080, 120, 8
    tags = (30, 220)
    rng = np.random.default_rng(0)
    # Per-eye tag block a decode reads back, over noise the encoder cannot collapse
    # to nothing. Held on the HOST: the upload inside the loop is the churn.
    host = []
    base = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)   # shared: only tags are read
    for tag in tags:
        frames = []
        for i in range(distinct):
            f = np.roll(base, i * 37, axis=1)
            f[:160, :160] = tag
            f[:160, 200:360] = 10 + i * 30   # which frame, not just which eye
            frames.append(f)
        host.append(frames)

    encs = [NvHevcEncoder(w, h, keyint=30, bitrate_kbps=8000, full_range=True)
            for _ in range(2)]
    aus: list[list[bytes]] = [[], []]
    try:
        for i in range(n):
            for e in (0, 1):
                frame = cupy.asarray(host[e][i % distinct])   # fresh device buffer
                aus[e] += [au for _t, au in encs[e].encode(frame, T0 + i * DT)]
    finally:
        # A consumer card caps concurrent NVENC sessions, so an assertion failure
        # must not leave two of them held for the rest of the module.
        for e in (0, 1):
            aus[e] += [au for _t, au in encs[e].flush()]

    for e, want in enumerate(tags):
        ctx = av.CodecContext.create("hevc", "r")
        # Frame threading: the decode dominates this test (measured 7.1 s -> 3.7 s).
        # It holds a tail, so the drain below is what keeps `len(got) == n` true.
        ctx.thread_type = "FRAME"
        ctx.thread_count = 0
        got = []
        for au in [*aus[e], None]:
            for frame in (ctx.decode(None) if au is None
                          else ctx.decode(av.Packet(au))):
                plane = frame.to_ndarray()[:h]
                got.append((float(plane[20:140, 20:140].mean()),
                            float(plane[20:140, 220:340].mean())))
        assert len(got) == n, f"eye {e} decoded {len(got)} of {n} frames"
        # Two tolerances, because the two blocks do not decode equally well. The eye
        # tag is flush in the corner and comes back within 1 code, so 5 is 5x margin.
        # The index tag has high-entropy noise on both sides and at 8 Mbps on 1080p
        # noise the rate control pins QP near its ceiling: measured 4.82 off, which
        # left 0.18 of headroom against a tolerance of 5. A 30-apart step with a
        # tolerance of 10 restores ~2x margin while keeping the index unambiguous —
        # a wrong index is >=30 away. Both stay tight enough to catch a PARTIALLY
        # overwritten block, which a loose threshold waves through.
        wrong = [(j, eye, idx) for j, (eye, idx) in enumerate(got)
                 if abs(eye - want) > 5 or abs(idx - (10 + (j % distinct) * 30)) > 10]
        shown = [(j, round(a), round(b)) for j, a, b in wrong[:8]]
        assert not wrong, (
            f"eye {e}: {len(wrong)} of {len(got)} frames carry the wrong picture; "
            f"wanted eye-tag {want}, got (frame, eye-tag, index-tag) {shown}")
