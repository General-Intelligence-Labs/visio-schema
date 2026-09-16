"""The rect/delivery encoder's knobs, and the line between delivery and depth.

`x265_params` is shared by the delivery encoder and the DEPTH encoder, whose luma
is the disparity measurement rather than a picture. Every test here that pins a
default is pinning that separation.
"""

from __future__ import annotations

import av
import numpy as np
import pytest

from visio_schema.reader._encode import HevcEncoder, x265_params

W, H = 192, 128
T0 = 1_700_000_000 * 1_000_000_000
DT = 33_000_000


def _frames(n):
    """`n` frames with moving content, so the encoder has something to predict."""
    out = []
    for i in range(n):
        img = np.zeros((H, W, 3), np.uint8)
        img[:, (i * 7) % W:((i * 7) % W) + 20] = 255
        out.append(img)
    return out


def _encode(frames, **kw):
    enc = HevcEncoder(W, H, keyint=10, **kw)
    buf, n = bytearray(), 0
    for i, f in enumerate(frames):
        for _t, au in enc.encode(f, T0 + i * DT):
            buf += au
            n += 1
    for _t, au in enc.flush():
        buf += au
        n += 1
    return bytes(buf), n


def test_depth_call_site_keeps_single_frame_thread():
    """The depth encoder's parameters must not move when a delivery knob is added.

    Its luma IS the disparity measurement, so frame-parallel encoding is not a
    free speed-up there the way it is for a picture.
    """
    p = x265_params(30, repeat_headers=True)
    assert "frame-threads=1" in p
    assert "repeat-headers=1" in p
    assert "fps=" not in p and "bitrate=" not in p


def test_rate_control_clause_is_opt_in():
    assert "bitrate=" not in x265_params(30)
    p = x265_params(30, bitrate_kbps=8000)
    # fps rides WITH rate control: ABR needs the frame rate, CRF does not.
    assert "bitrate=8000" in p and "fps=30" in p
    assert "vbv-maxrate=12000" in p and "vbv-bufsize=12000" in p


def test_frame_threads_is_a_parameter_not_a_constant():
    assert "frame-threads=1" in x265_params(30)
    assert "frame-threads=0" in x265_params(30, frame_threads=0)


def test_preset_is_a_separate_option_never_an_x265_param():
    """x265's parameter parser does not know `preset` and ignores it silently —
    as a codec option it reaches `x265_param_default_preset` instead."""
    enc = HevcEncoder(W, H, keyint=10, preset="faster")
    assert enc._ctx.options["preset"] == "faster"
    assert "preset" not in enc._ctx.options["x265-params"]
    assert "preset" not in HevcEncoder(W, H, keyint=10, preset=None)._ctx.options


@pytest.mark.parametrize("kw", [
    {},                                                   # delivery defaults
    {"preset": "faster", "frame_threads": 0, "bitrate_kbps": 4000},
    {"preset": None, "frame_threads": 1},                 # the reference point
])
def test_delivery_contract_survives_every_rung(kw, tmp_path):
    """No B-frames and a GOP no longer than `keyint`, whatever the preset.

    The preset is applied BEFORE `x265-params` is parsed, so the explicit
    `bframes=0`/`keyint` still win — this is the test that says so rather than
    trusting the ordering.
    """
    frames = _frames(24)
    data, n_au = _encode(frames, **kw)
    assert n_au == len(frames)  # one access unit per frame, no reorder

    path = tmp_path / "c.hevc"
    path.write_bytes(data)
    with av.open(str(path), format="hevc") as c:
        st = c.streams.video[0]
        assert st.codec_context.has_b_frames is False
        gap = longest = 0
        for pkt in c.demux(video=0):
            if not pkt.size:
                continue
            gap += 1
            if pkt.is_keyframe:
                longest = max(longest, gap)
                gap = 0
        assert max(longest, gap) <= 10


def _flat(value):
    """A frame of one grey level — the only shape that pins a conversion exactly."""
    return np.full((H, W, 3), value, np.uint8)


def _luma(frames, **kw) -> list[int]:
    """Encode ``frames``, then the median Y of each, off the coded planes.

    `to_ndarray()` with no format argument is deliberate: asking for rgb24 would
    run the samples back through swscale using the very tag under test, which
    turns a mislabelled stream into a self-consistent one and hides the bug.
    """
    enc = HevcEncoder(W, H, keyint=10, **kw)
    aus = []
    for i, f in enumerate(frames):
        aus += [au for _t, au in enc.encode(f, T0 + i * DT)]
    aus += [au for _t, au in enc.flush()]
    ctx = av.CodecContext.create("hevc", "r")
    out = []
    for au in aus:
        for frame in ctx.decode(av.Packet(au)):
            out.append(int(np.median(frame.to_ndarray()[:H])))
    return out


def test_full_range_converts_the_samples_not_just_the_tag():
    """The tag alone was the bug: swscale converts RGB -> YUV at LIMITED range
    whatever the frame says it is, so setting `color_range` shipped 16-235 samples
    labelled 0-255 and a decoder honouring the tag expanded them twice."""
    grey = [_flat(0), _flat(128), _flat(255)]
    assert _luma(grey, full_range=True) == [0, 128, 255]
    # The default is unchanged — this encoder is shared, and moving it would move
    # every existing consumer's pixels.
    assert _luma(grey) == [16, 126, 235]


def test_full_range_declares_bt709_because_the_default_matrix_is_601():
    """A range without a matrix is still a stream a consumer has to guess at. The
    recorder signals BT.709; swscale's default here is BT.601, which moves every
    colour (pure red: Y 54 under 709 full, Y 81 under 601 limited)."""
    red = np.zeros((H, W, 3), np.uint8)
    red[..., 0] = 255
    assert _luma([red], full_range=True) == [54]
    enc = HevcEncoder(W, H, keyint=10, full_range=True)
    assert enc._ctx.color_range == 2                 # AVCOL_RANGE_JPEG
    assert enc._ctx.colorspace == 1                  # AVCOL_SPC_BT709
    assert enc._ctx.color_primaries == 1
    assert enc._ctx.color_trc == 1
    assert HevcEncoder(W, H, keyint=10)._ctx.color_range == 1   # default untouched


def test_full_range_reaches_the_stream_not_just_the_context(tmp_path):
    for full_range, want in ((True, "yuvj420p"), (False, "yuv420p")):
        data, _ = _encode(_frames(12), full_range=full_range)
        path = tmp_path / f"{full_range}.hevc"
        path.write_bytes(data)
        with av.open(str(path), format="hevc") as c:
            for frame in c.decode(video=0):
                assert frame.format.name == want
                break


def test_full_range_is_not_refused_on_the_gpu_arm():
    """`full_range` describes the STREAM, so it has to survive whichever encoder
    runs. NVENC reaches it by being handed NV12 we converted ourselves plus an
    inserted VUI — not by a config knob, which PyNvVideoCodec does not expose."""
    import logging

    from visio_schema.reader import make_rect_encoder
    enc = make_rect_encoder(W, H, keyint=10, choice="cpu", gpu_backend=False,
                            log=logging.getLogger(), full_range=True)
    assert enc._ctx.color_range == 2
    # The gpu arm must not raise on the combination. Without a GPU present it
    # degrades to libx265 with the same request intact, which is the property that
    # matters here; `test_encode_gpu.py` covers the NVENC samples themselves.
    gpu = make_rect_encoder(W, H, keyint=10, choice="auto", gpu_backend=True,
                            log=logging.getLogger(), full_range=True)
    assert gpu.codec_name in ("nvenc-hevc", "libx265")
    if gpu.codec_name == "libx265":
        assert gpu._ctx.color_range == 2


def test_the_depth_encoder_is_untouched_by_the_delivery_knobs():
    """Not just `x265_params`' defaults — the depth encoder ITSELF, so a future
    copy-paste of the delivery knobs into it is caught."""
    from visio_schema.reader._encode import HevcDepthEncoder

    enc = HevcDepthEncoder(W, H, keyint=30)
    params = enc._ctx.options["x265-params"]
    assert "frame-threads=1" in params and "repeat-headers=1" in params
    assert "preset" not in enc._ctx.options
    assert "fps=" not in params and "bitrate=" not in params


def test_insert_colour_vui_adds_the_signalling_nvenc_omits():
    """Host-runnable on purpose: only OBTAINING NVENC's parameter sets needs a
    device, and the insertion is the half that can silently do nothing.

    libx265's own limited-range parameter sets stand in for NVENC's — both arrive
    without full-range signalling, which is exactly the input this must fix."""
    from visio_schema.reader._encode import insert_colour_vui

    limited, _ = _encode(_frames(2))                  # yuv420p, no full-range flag
    patched = insert_colour_vui(limited, W, H)
    assert patched != limited

    ctx = av.CodecContext.create("hevc", "r")
    for frame in ctx.decode(av.Packet(patched)):
        assert frame.format.name == "yuvj420p"        # full range now declared
        assert int(frame.color_range) == 2
        assert int(frame.colorspace) == 1             # BT.709
        break
    else:
        pytest.fail("the patched parameter sets decoded no frame")


# Deliberately NOT `W, H`: there `H * 3 // 2 == W == 192`, so a ring allocated at the
# frame's geometry instead of NV12's would be indistinguishable from the right one.
RW, RH = 256, 128


@pytest.fixture
def fake_gpu(monkeypatch):
    """The three GPU modules, faked, recording every allocation, fill and free.

    Host-runnable ON PURPOSE. `test_encode_gpu.py` proves these properties on real
    hardware, but visio-schema CI has no GPU runner, so all of it skips there and
    guards nothing. Everything `NvHevcEncoder` does to its input surfaces is
    bookkeeping over three vendor calls, and bookkeeping is checkable without a card.
    """
    import sys
    import types

    rec = types.SimpleNamespace(allocs=[], fills=[], freed=[], made=[], created={},
                               cleared=[])

    class _Stream:
        def __init__(self, **kw):
            self.ptr, self.depth, self.kw = 0xBEEF, 0, kw
            rec.made.append(self)

        def __enter__(self):
            self.depth += 1
            return self

        def __exit__(self, *exc):
            self.depth -= 1
            return False

    class _Buf:
        def __init__(self, shape, dtype):
            self.shape, self.dtype = shape, dtype

        def __getitem__(self, _key):
            return self

    def _empty(shape, dtype, *a, **k):
        rec.allocs.append(_Buf(shape, dtype))
        return rec.allocs[-1]

    cupy = types.ModuleType("cupy")
    cupy.uint8 = np.uint8
    cupy.empty = _empty
    cupy.asarray = lambda x: x
    cupy.get_default_memory_pool = lambda: types.SimpleNamespace(
        free_all_blocks=lambda stream=None: rec.freed.append(stream))
    cupy.cuda = types.SimpleNamespace(
        Stream=_Stream,
        driver=types.SimpleNamespace(ctxGetCurrent=lambda: 0xC7),
        # cupy's default stream really is 0 — the trap the fix exists for.
        get_current_stream=lambda: types.SimpleNamespace(ptr=0),
    )
    cvcuda = types.ModuleType("cvcuda")
    cvcuda.as_tensor = lambda buf, layout: ("tensor", buf)
    cvcuda.ThreadScope = types.SimpleNamespace(LOCAL="local")
    cvcuda.clear_cache = lambda scope: rec.cleared.append(scope)
    nvc = types.ModuleType("PyNvVideoCodec")
    nvc.CreateEncoder = lambda *a, **k: rec.created.update(
        k, opened_inside=rec.made[-1].depth) or types.SimpleNamespace(
        Encode=lambda _t: [], EndEncode=lambda: [])
    monkeypatch.setitem(sys.modules, "cupy", cupy)
    monkeypatch.setitem(sys.modules, "cvcuda", cvcuda)
    monkeypatch.setitem(sys.modules, "PyNvVideoCodec", nvc)

    from visio_schema.reader import _gpu_color
    monkeypatch.setattr(
        _gpu_color, "rgb_to_nv12",
        lambda src, *, full_range, out=None: rec.fills.append(out) or out)
    return rec


def test_nvenc_binds_a_cuda_stream_of_its_own(fake_gpu):
    """The delivery bug was a single constructor argument: `cudastream` was handed
    `cupy.cuda.get_current_stream().ptr`, which is 0 on the default stream, and
    PyNvVideoCodec reads 0 as "not provided". NVENC then read its input surface
    unordered against the cupy kernels filling it.

    A shape guard, not a semantics one — but the shape is exactly what broke.
    """
    from visio_schema.reader._encode import NvHevcEncoder

    NvHevcEncoder(RW, RH, keyint=10)
    got = fake_gpu.created

    assert got["cudastream"] not in (0, None), "NVENC was bound to no stream at all"
    assert got["cudastream"] != 0, (
        "NVENC was handed the default stream's ptr, which PyNvVideoCodec reads as "
        "'not provided'")
    assert got["opened_inside"] == 1, "the session was opened outside its own stream"
    assert fake_gpu.made[0].kw == {"non_blocking": False}, (
        "a non-blocking stream drops the implicit sync with the legacy default "
        "stream, moving the ordering burden onto every caller")


def test_the_nv12_ring_is_one_allocation_per_encoder_not_one_per_frame(fake_gpu):
    """The regression the ring exists for: a per-frame `cupy.empty` returns the block
    to the process-wide pool between frames, where the OTHER eye's encoder can be
    handed it. Counting allocations is the only thing that says so — two rings being
    disjoint is true under any fake, pool or no pool.
    """
    from visio_schema.reader._encode import NvHevcEncoder

    enc = NvHevcEncoder(RW, RH, keyint=10)
    after_construction = len(fake_gpu.allocs)
    frame = np.zeros((RH, RW, 3), np.uint8)
    for i in range(3 * NvHevcEncoder._INFLIGHT):
        enc.encode(frame, T0 + i * DT)
    assert len(fake_gpu.allocs) == after_construction, (
        "`encode` allocated a device buffer; the ring is meant to be the only one")
    # At NV12 geometry, not the frame's: a (H, W) ring is a buffer NVENC reads two
    # thirds of a picture out of.
    assert [(b.shape, b.dtype) for b in enc._ring] == (
        [((RH * 3 // 2, RW), np.uint8)] * NvHevcEncoder._INFLIGHT)


def test_the_ring_rotates_through_every_slot_and_wraps(fake_gpu):
    """Rotation is the mechanism: a slot that comes round again before NVENC has
    finished with it is a stale read, and a `_slot` that never advances reuses slot 0
    for every frame — which no assertion on NVENC's OUTPUT detects.
    """
    from visio_schema.reader._encode import NvHevcEncoder

    enc = NvHevcEncoder(RW, RH, keyint=10)
    n = 2 * NvHevcEncoder._INFLIGHT + 1
    frame = np.zeros((RH, RW, 3), np.uint8)
    for i in range(n):
        enc.encode(frame, T0 + i * DT)
    want = [enc._ring[i % NvHevcEncoder._INFLIGHT] for i in range(n)]
    assert [id(b) for b in fake_gpu.fills] == [id(b) for b in want]
    assert len({id(b) for b in fake_gpu.fills}) == NvHevcEncoder._INFLIGHT
    # A literal floor, because every assertion above is derived from `_INFLIGHT` and
    # so passes at `_INFLIGHT = 1`. NVENC's pipeline is ~4 frames deep (measured, both
    # tunings); at or below that a slot comes round while NVENC is still reading it.
    assert NvHevcEncoder._INFLIGHT >= 6


def test_flush_gives_the_ring_back_on_the_encoders_own_stream(fake_gpu):
    """`pack` builds an encoder pair PER CLIP, and cupy keys its free lists by the
    allocating stream: a ring left referenced stays cached on a stream nothing else
    can allocate from.
    """
    from visio_schema.reader._encode import NvHevcEncoder

    enc = NvHevcEncoder(RW, RH, keyint=10)
    stream = enc._stream
    enc.flush()
    assert enc._ring == [], "the ring outlived the session"
    assert fake_gpu.freed == [stream], (
        "the per-stream arena was not reclaimed on the encoder's own stream")
    # Without this the last slot stays pinned by cvcuda's cached tensor and is
    # stranded for good, because the next clip frees only its own stream's arena.
    assert fake_gpu.cleared == ["local"], (
        "cvcuda's cache still holds a tensor wrapping a ring slot")
