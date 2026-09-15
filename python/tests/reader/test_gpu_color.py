"""`rgb_to_nv12` against a reference, chroma included.

Everything else in the change set that touches this kernel asserts a median LUMA,
which a U/V swap passes cleanly — red delivered as blue, every test green. So the
chroma plane gets its own arithmetic check here, with no codec in the loop.
"""

from __future__ import annotations

import numpy as np
import pytest

cupy = pytest.importorskip("cupy", reason="the NV12 kernels need the gpu wheels")

from visio_schema.reader._gpu_color import rgb_to_nv12  # noqa: E402

H, W = 64, 96


def reference_nv12(rgb: np.ndarray, *, full_range: bool) -> np.ndarray:
    """NV12 the slow, obvious way: float maths, 2x2 box chroma, no kernels."""
    kr, kg, kb = (0.2126, 0.7152, 0.0722) if full_range else (0.299, 0.587, 0.114)
    y_scale, y_off = (1.0, 0.0) if full_range else (219.0 / 255.0, 16.0)
    c_scale = 1.0 if full_range else 224.0 / 255.0
    f = rgb.astype(np.float64)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    y = (kr * r + kg * g + kb * b) * y_scale + y_off

    # Chroma from the 2x2 box average of the SOURCE RGB.
    box = f.reshape(H // 2, 2, W // 2, 2, 3).mean(axis=(1, 3))
    br, bg, bb = box[..., 0], box[..., 1], box[..., 2]
    by = kr * br + kg * bg + kb * bb
    u = (bb - by) / (2 * (1 - kb)) * c_scale + 128.0
    v = (br - by) / (2 * (1 - kr)) * c_scale + 128.0

    out = np.empty((H * 3 // 2, W), np.uint8)
    out[:H] = np.clip(np.rint(y), 0, 255)
    uv = out[H:].reshape(H // 2, W // 2, 2)
    uv[..., 0] = np.clip(np.rint(u), 0, 255)   # U first
    uv[..., 1] = np.clip(np.rint(v), 0, 255)   # then V
    return out


def _run(rgb, *, full_range):
    return cupy.asnumpy(rgb_to_nv12(cupy.asarray(rgb), full_range=full_range))


@pytest.mark.parametrize("full_range", [True, False])
def test_matches_the_reference_over_a_random_image(full_range):
    rng = np.random.default_rng(7)
    rgb = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
    got = _run(rgb, full_range=full_range)
    want = reference_nv12(rgb, full_range=full_range)
    # +/-1 is float-vs-float32 rounding at the .5 boundary, not a different formula.
    assert np.abs(got.astype(int) - want.astype(int)).max() <= 1


def test_u_and_v_are_not_swapped():
    """The check the luma assertions cannot make. Saturated red and saturated blue
    sit at opposite ends of both chroma axes, so a swap is unmissable here."""
    red = np.zeros((H, W, 3), np.uint8)
    red[..., 0] = 255
    blue = np.zeros((H, W, 3), np.uint8)
    blue[..., 2] = 255

    red_uv = _run(red, full_range=True)[H:].reshape(H // 2, W // 2, 2)
    blue_uv = _run(blue, full_range=True)[H:].reshape(H // 2, W // 2, 2)
    red_u, red_v = int(np.median(red_uv[..., 0])), int(np.median(red_uv[..., 1]))
    blue_u, blue_v = int(np.median(blue_uv[..., 0])), int(np.median(blue_uv[..., 1]))

    # BT.709 full range: V carries red, U carries blue.
    assert red_v == 255 and red_u < 128
    assert blue_u == 255 and blue_v < 128


def test_chroma_is_the_box_average_not_a_subsample():
    """A 2x2 checkerboard averages to a flat mid-grey in chroma. Point-sampling one
    corner instead would return the corner's own colour."""
    rgb = np.zeros((H, W, 3), np.uint8)
    rgb[0::2, 0::2, 0] = 255   # red on one diagonal of every 2x2 block
    rgb[1::2, 1::2, 2] = 255   # blue on the other
    uv = _run(rgb, full_range=True)[H:].reshape(H // 2, W // 2, 2)
    want = reference_nv12(rgb, full_range=True)[H:].reshape(H // 2, W // 2, 2)
    assert np.abs(uv.astype(int) - want.astype(int)).max() <= 1
    # and it is genuinely averaged: neither pure red's V nor pure blue's U
    assert 128 < int(np.median(uv[..., 0])) < 255
    assert 128 < int(np.median(uv[..., 1])) < 255


def test_limited_range_puts_luma_where_nvenc_put_it():
    """The limited arm must keep the picture the ABGR path produced: BT.601, 16-235."""
    for value, want_y in ((0, 16), (128, 126), (255, 235)):
        flat = np.full((H, W, 3), value, np.uint8)
        assert int(np.median(_run(flat, full_range=False)[:H])) == want_y


def test_full_range_spans_the_whole_scale():
    for value in (0, 128, 255):
        flat = np.full((H, W, 3), value, np.uint8)
        assert int(np.median(_run(flat, full_range=True)[:H])) == value


@pytest.mark.parametrize("shape", [(H, W), (H, W, 4), (H + 1, W, 3), (H, W + 1, 3)])
def test_a_shape_the_kernels_cannot_index_is_refused(shape):
    """The kernels index with hand-computed offsets and no bounds check, so these
    would read past the buffer rather than fail."""
    with pytest.raises(ValueError):
        rgb_to_nv12(cupy.zeros(shape, cupy.uint8), full_range=True)
