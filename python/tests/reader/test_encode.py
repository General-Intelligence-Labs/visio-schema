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


def test_full_range_declares_what_the_samples_are():
    """swscale writes full-range luma out of RGB whatever the tag says, so the tag
    has to be set deliberately or the stream lies about its own samples."""
    full = HevcEncoder(W, H, keyint=10, full_range=True)
    assert full._ctx.color_range == 2          # AVCOL_RANGE_JPEG
    limited = HevcEncoder(W, H, keyint=10)
    assert limited._ctx.color_range == 1       # the default stays today's behaviour


def test_full_range_reaches_the_stream_not_just_the_context(tmp_path):
    for full_range, want in ((True, "yuvj420p"), (False, "yuv420p")):
        data, _ = _encode(_frames(12), full_range=full_range)
        path = tmp_path / f"{full_range}.hevc"
        path.write_bytes(data)
        with av.open(str(path), format="hevc") as c:
            for frame in c.decode(video=0):
                assert frame.format.name == want
                break


def test_nvenc_cannot_declare_full_range_so_the_pair_is_refused():
    """`full_range` describes the STREAM, so honouring it on one backend and
    dropping it on the other would make a delivery's colour range depend on which
    machine encoded it. NVENC has no colour-range control, so ask and it refuses."""
    import logging

    from visio_schema.reader import make_rect_encoder
    for kw in ({"choice": "gpu", "gpu_backend": False},
               {"choice": "auto", "gpu_backend": True}):
        with pytest.raises(ValueError, match="cannot be honoured by NVENC"):
            make_rect_encoder(W, H, keyint=10, log=logging.getLogger(),
                              full_range=True, **kw)
    # the cpu arm carries it through
    enc = make_rect_encoder(W, H, keyint=10, choice="cpu", gpu_backend=False,
                            log=logging.getLogger(), full_range=True)
    assert enc._ctx.color_range == 2


def test_the_depth_encoder_is_untouched_by_the_delivery_knobs():
    """Not just `x265_params`' defaults — the depth encoder ITSELF, so a future
    copy-paste of the delivery knobs into it is caught."""
    from visio_schema.reader._encode import HevcDepthEncoder

    enc = HevcDepthEncoder(W, H, keyint=30)
    params = enc._ctx.options["x265-params"]
    assert "frame-threads=1" in params and "repeat-headers=1" in params
    assert "preset" not in enc._ctx.options
    assert "fps=" not in params and "bitrate=" not in params
