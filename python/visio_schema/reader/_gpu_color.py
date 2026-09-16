"""Device-side RGB -> NV12, the conversion NVENC will not do for us.

NVENC has no 24-bit packed RGB input at all — its formats are ``NV12, YUV420,
ARGB, ABGR, YUV444, P010, YUV444_10BIT, YUV444_16BIT, NV16, P210`` — so an RGB
caller has to pick one. Handing it ``ABGR`` (pad every pixel to 4 bytes and let
the driver convert) is the obvious choice and the wrong one, twice over:

- **It cannot produce a full-range stream.** For packed-RGB input the driver
  converts with a fixed BT.470BG limited-range matrix and forces
  ``videoFullRangeFlag`` to 0; FFmpeg's own ``nvenc.c`` special-cases exactly
  this. Measured here: RGB 0/255 -> Y 16/235, whatever the config says.
- **The padding is not free on the host.** The numpy ``abgr[..., :3] = rgb``
  pack measured **20.8 ms/frame** at 1080p, against NVENC's own encode of
  **0.83 ms** — twenty-five times the cost of the thing it was feeding.

So we convert ourselves, on the device, and hand NVENC NV12. That makes the
matrix and the range OURS to choose, which is the whole point: the recorder
signals full-range BT.709 (``color_range=pc, color_space=bt709``) and a delivery
that re-encodes its picture should say the same thing.

``cvcuda.cvtcolor(RGB2YUV_NV12)`` is not usable for the full-range case — it is
limited range (measured RGB 0/255 -> Y 16/235), same as everything else.

Both kernels are fused: a vectorised cupy expression would allocate half a dozen
full-resolution float32 temporaries per frame, which at these frame rates costs
more than the encode.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # device arrays; only type hints, no import at module load
    import cupy

# BT.709, the full-range arm's matrix — what the recorder signals.
_KR, _KG, _KB = 0.2126, 0.7152, 0.0722
# BT.601 (== AVCOL_SPC_BT470BG) for the limited arm: that is what NVENC's own
# packed-RGB conversion produced, so existing reference-video callers keep the
# picture they had.
_KR601, _KG601, _KB601 = 0.299, 0.587, 0.114


@functools.cache
def _build(full: bool):
    """Compile (and cache) the Y and UV kernels for one range.

    The matrix is not independently selectable: a full-range delivery is BT.709
    and the limited reference video is BT.601, so one flag picks both.
    """
    import cupy

    kr, kg, kb = (_KR, _KG, _KB) if full else (_KR601, _KG601, _KB601)
    # Full range writes 0-255 directly; limited compresses luma into 16-235 and
    # chroma into 16-240, the same scaling swscale and NVENC apply.
    y_scale, y_off = (1.0, 0.0) if full else (219.0 / 255.0, 16.0)
    c_scale = 1.0 if full else 224.0 / 255.0
    u_div, v_div = 2.0 * (1.0 - kb), 2.0 * (1.0 - kr)
    consts = (f"const float KR={kr}f, KG={kg}f, KB={kb}f;"
              f"const float YS={y_scale}f, YO={y_off}f, CS={c_scale}f;"
              f"const float UD={u_div}f, VD={v_div}f;")
    y_kernel = cupy.ElementwiseKernel(
        "raw uint8 rgb", "uint8 y",
        consts + """
        const int p = i * 3;
        const float R = rgb[p], G = rgb[p + 1], B = rgb[p + 2];
        const float yf = (KR * R + KG * G + KB * B) * YS + YO;
        y = (unsigned char)__float2int_rn(fminf(fmaxf(yf, 0.0f), 255.0f));
        """,
        f"visio_rgb2y_{'full' if full else 'lim'}")
    uv_kernel = cupy.ElementwiseKernel(
        "raw uint8 rgb, int32 W", "raw uint8 uv",
        consts + """
        const int cw = W / 2;
        const int cx = i % cw, cy = i / cw;
        const int x0 = cx * 2, y0 = cy * 2;
        float R = 0.0f, G = 0.0f, B = 0.0f;
        for (int dy = 0; dy < 2; ++dy) {
            for (int dx = 0; dx < 2; ++dx) {
                const int p = ((y0 + dy) * W + (x0 + dx)) * 3;
                R += rgb[p]; G += rgb[p + 1]; B += rgb[p + 2];
            }
        }
        R *= 0.25f; G *= 0.25f; B *= 0.25f;
        const float Y = KR * R + KG * G + KB * B;
        const float u = (B - Y) / UD * CS + 128.0f;
        const float v = (R - Y) / VD * CS + 128.0f;
        uv[i * 2]     = (unsigned char)__float2int_rn(fminf(fmaxf(u, 0.0f), 255.0f));
        uv[i * 2 + 1] = (unsigned char)__float2int_rn(fminf(fmaxf(v, 0.0f), 255.0f));
        """,
        f"visio_rgb2uv_{'full' if full else 'lim'}")
    return y_kernel, uv_kernel


def rgb_to_nv12(rgb, *, full_range: bool,
                out: cupy.ndarray | None = None) -> cupy.ndarray:
    """Device RGB ``(H, W, 3)`` uint8 -> NV12 ``(H * 3 // 2, W)`` uint8.

    ``full_range`` picks BT.709 full range (the delivery) or BT.601 limited (the
    reference video). Chroma is the 2x2 box average of the source RGB: averaging
    RGB and then converting is identical to converting and then averaging (the
    transform is linear), so this matches swscale up to rounding.

    ``out`` writes into a caller-owned buffer instead of allocating one: NVENC reads
    its input surface after ``Encode`` returns, so only the caller knows when a
    surface is free to reuse. ``None`` allocates, which is the reference path.
    """
    import cupy

    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"rgb_to_nv12: expected (H, W, 3) uint8, got {rgb.shape}")
    h, w = rgb.shape[:2]
    if h % 2 or w % 2:
        # The kernels index with hand-computed offsets and no bounds check, so an
        # odd edge would read past the buffer rather than fail.
        raise ValueError(
            f"rgb_to_nv12: 4:2:0 needs even dimensions, got {w}x{h}")
    y_kernel, uv_kernel = _build(full_range)

    # Load-bearing, not a formality: the kernels take `raw uint8` and index with
    # hand-computed offsets, so they ignore strides entirely — a padded or strided
    # input would be silently transposed into garbage rather than raising. It is a
    # reference return for every producer we have (NVDEC and cvcuda both hand back
    # packed buffers), so it costs nothing in the normal case.
    src = cupy.ascontiguousarray(rgb)
    shape = (h * 3 // 2, w)
    if out is None:
        out = cupy.empty(shape, cupy.uint8)
    elif out.shape != shape or out.dtype != cupy.uint8:
        # Same reason the kernels demand a contiguous source: they index with raw
        # offsets, so a mis-shaped destination is written past rather than rejected.
        raise ValueError(
            f"rgb_to_nv12: out must be {shape} uint8, got {out.shape} {out.dtype}")
    y_kernel(src, out[:h])
    uv_kernel(src, cupy.int32(w), out[h:], size=(h // 2) * (w // 2))
    return out
