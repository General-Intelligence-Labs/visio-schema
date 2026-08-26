"""H.265 access-unit **encoder** — the write-side counterpart to ``_decode.py``.

Turns rectified RGB frames into per-frame Annex-B H.265 for a
``foxglove.CompressedVideo`` sidecar: **one frame per message, no B-frames**, so a
consumer decodes it the same 1-in-1-out way the recording's camera stream decodes.
Two backends behind one interface, picked by ``make_rect_encoder``:

- ``HevcEncoder``   — libx265 via PyAV (CPU): the reproducible reference, and the
  path the ``--backend cpu`` reference uses. Needs no GPU wheels.
- ``NvHevcEncoder`` — NVENC via ``PyNvVideoCodec`` (GPU): offloads the encode to the
  video engine so it never competes with the depth engine for CPU/GPU compute.

**Both take a HOST ``(H, W, 3)`` uint8 RGB frame** and return ``[(t_ns, au_bytes)]``.
NVENC is fed through its *CPU input buffer* (``usecpuinputbuffer=True``): it copies
the host frame into its own managed surface synchronously, which sidesteps the
device-surface-lifetime trap — NVENC runs a ~3-frame async pipeline, so a caller-owned
device buffer would be recycled out from under it before the encoder reads it. The one
extra device→host copy on the GPU backend is cheap (~2 MB); the heavy lifting still
runs on NVENC silicon.


Counterpart of ``_decode``: the same codec table, and the x265 params here
(keyint, bframes=0, rc-lookahead=0, frame-threads=1) exist to emit exactly the
deterministic 1:1 Annex-B stream that decoder expects. It lives beside the
decoder rather than in a consumer so the two cannot drift — a second copy of
these params is a silent-corruption bug, and both this repo's tests and
visio-pp's need to synthesize a stream the reader will accept.
"""

from __future__ import annotations

import collections
import logging
from collections.abc import Callable
from typing import Literal

import numpy as np


class _FifoEncoder:
    """Pairs each emitted access unit to the oldest un-emitted input ``t_ns``.

    Both encoders have pipeline latency (``encode`` may return 0 packets for a call
    and several on a later one), but with ``bframes=0`` the output order equals the
    input order — so the k-th emitted AU belongs to the k-th input frame's ``t_ns``.
    Getting this pairing wrong is the drift bug ``_decode.py`` cites (~7.7 s). One
    deque, one home. ``flush`` drains the in-flight tail at end of stream.
    """

    def __init__(self) -> None:
        self._pending: collections.deque[int] = collections.deque()

    def _pair(self, packets, to_bytes: Callable[[object], bytes]
              ) -> list[tuple[int, bytes]]:
        return [(self._pending.popleft(), to_bytes(p)) for p in packets]


class HevcEncoder(_FifoEncoder):
    """libx265 (PyAV): host RGB -> Annex-B H.265, one packet per frame, no B-frames."""

    codec_name = "libx265"

    def __init__(self, width: int, height: int, *, keyint: int = 30) -> None:
        super().__init__()
        from fractions import Fraction

        import av

        self._ctx = av.CodecContext.create("libx265", "w")
        self._ctx.width, self._ctx.height = width, height
        self._ctx.pix_fmt = "yuv420p"
        self._ctx.time_base = Fraction(1, 30)
        # keyint => periodic IDR (seekable); bframes=0 => no reorder (1:1, in order);
        # rc-lookahead=0 + frame-threads=1 => low-latency, deterministic emission.
        self._ctx.options = {
            "x265-params": (
                f"log-level=none:keyint={keyint}:min-keyint={keyint}:"
                "bframes=0:rc-lookahead=0:scenecut=0:frame-threads=1"
            )
        }
        self._idx = 0

    def encode(self, rgb: np.ndarray, t_ns: int) -> list[tuple[int, bytes]]:
        import av

        self._pending.append(int(t_ns))
        vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
        vf = vf.reformat(format="yuv420p")
        vf.pts = self._idx  # monotonic counter so PyAV doesn't invent a pts
        self._idx += 1
        return self._pair(self._ctx.encode(vf), bytes)

    def flush(self) -> list[tuple[int, bytes]]:
        return self._pair(self._ctx.encode(None), bytes)


class NvHevcEncoder(_FifoEncoder):
    """NVENC (PyNvVideoCodec): host RGB -> Annex-B H.265 on the video engine.

    Lazy GPU dep (``PyNvVideoCodec``): imported only when constructed, so the CPU-only
    SDK import path stays free of GPU wheels. Raises on construction if NVENC can't
    initialise (missing wheel, no encode-capable GPU, session cap) — ``make_rect_encoder``
    catches that and falls back to libx265.
    """

    codec_name = "nvenc-hevc"

    def __init__(self, width: int, height: int, *, keyint: int = 30,
                 preset: str = "P3") -> None:
        super().__init__()
        import PyNvVideoCodec as nvc

        # NVENC packed-RGB input is ABGR: a 32-bit word read as bytes [R, G, B, A]
        # (verified by a red/blue round-trip). usecpuinputbuffer=True => NVENC copies
        # this host buffer into its own surface synchronously (no device-lifetime trap).
        self._enc = nvc.CreateEncoder(
            width, height, "ABGR", True,
            codec="hevc", preset=preset, tuning_info="ultra_low_latency",
            bf=0, gop=keyint,
        )
        self._abgr = np.empty((height, width, 4), np.uint8)  # reused host scratch
        self._abgr[..., 3] = 255

    @staticmethod
    def _au(d) -> bytes:
        return bytes(d["data"])

    def encode(self, rgb: np.ndarray, t_ns: int) -> list[tuple[int, bytes]]:
        self._pending.append(int(t_ns))
        self._abgr[..., :3] = rgb  # host RGB -> ABGR bytes [R, G, B, 255]
        return self._pair(self._enc.Encode(self._abgr), self._au)

    def flush(self) -> list[tuple[int, bytes]]:
        return self._pair(self._enc.EndEncode(), self._au)


def make_rect_encoder(
    width: int, height: int, *, keyint: int,
    choice: Literal["auto", "gpu", "cpu"], gpu_backend: bool,
    log: logging.Logger,
) -> HevcEncoder | NvHevcEncoder:
    """Pick the rect-video H.265 encoder.

    ``choice`` is ``"auto" | "gpu" | "cpu"``: ``"auto"`` => NVENC on the gpu backend,
    libx265 on the cpu backend; ``"gpu"`` forces NVENC; ``"cpu"`` forces libx265. If
    NVENC is requested but cannot initialise, warn and fall back to libx265, so the
    stage never hard-fails on a missing/capped NVENC session.
    """
    want_gpu = choice == "gpu" or (choice == "auto" and gpu_backend)
    if want_gpu:
        try:
            return NvHevcEncoder(width, height, keyint=keyint)
        except Exception as e:
            log.warning("NVENC unavailable (%s); rect video falls back to libx265", e)
    return HevcEncoder(width, height, keyint=keyint)


# --------------------------------------------------------------------------- #
# Depth: disparity coded as HEVC Main 10 4:2:0
#
# A `mono16` millimetre depth map spends 16 bits/px on precision the matcher does
# not have — at 2 m one 1 mm LSB is 0.007 px of disparity — so its low bits are
# noise, and noise is why generic compression stalls at ~3.4x. Coding DISPARITY
# instead, whose error is uniform in px, reaches ~14x at crf 6.
#
# **4:2:0, not monochrome, and this is measured rather than chosen.** Played in
# Foxglove on real frames: HEVC Monochrome-12 (Range Extensions) does not render,
# and neither does AV1 Main monochrome, while HEVC Main 10 4:2:0 and H.264 8-bit
# 4:2:0 both do. The blocker is the CHROMA FORMAT — nothing in that stack decodes
# 4:0:0. Monochrome buys nothing anyway: flat chroma measured 41.06 vs 41.09
# KiB/frame against gray, so the two extra planes are free.
#
# 10 bits is likewise forced: HEVC Main 12 is Range Extensions too, so `192 * 4 =
# 768` fits 1023 while `192 * 8` does not. Quantization contributes ~0.12 px
# against the codec's ~0.3 px, so `crf` is the limiter, not the grid.
# --------------------------------------------------------------------------- #

_log = logging.getLogger("visio_schema.reader.encode")

DEPTH_PIX_FMT = "yuv420p10le"
DEPTH_BITS = 10
DEPTH_DISPARITY_FRAC = 4.0            # the 1/4 px grid
_DEPTH_MAX_CODE = (1 << DEPTH_BITS) - 1
_DEPTH_NEUTRAL_CHROMA = 1 << (DEPTH_BITS - 1)   # 512 — grey, in SAMPLES not bytes


def quantize_disparity(
    disparity: np.ndarray, *, frac: float = DEPTH_DISPARITY_FRAC
) -> np.ndarray:
    """Disparity px -> the 10-bit luma codes a depth stream carries.

    One home for the grid, so the encoders, the stage's verify harness and any
    consumer computing ``depth_scale`` cannot disagree about it.

    A disparity above the ceiling is REPORTED, not quietly saturated: it means the
    engine's ``max_disp`` and this grid disagree, which silently flattens the near
    field rather than raising.
    """
    q = np.round(np.asarray(disparity, np.float32) * frac)
    over = int(np.count_nonzero(q > _DEPTH_MAX_CODE))
    if over:
        _log.warning(
            "%d px above the 1/%g-px %d-bit ceiling (%.1f px) were clipped — the "
            "engine's max_disp and this grid disagree",
            over, frac, DEPTH_BITS, _DEPTH_MAX_CODE / frac,
        )
    return np.clip(q, 0, _DEPTH_MAX_CODE).astype(np.uint16)


def _fill_plane(plane, arr: np.ndarray) -> None:
    """Copy a ``(h, w)`` array into an ``av`` plane, honouring its ``line_size``.

    Planes carry row padding, so a flat memcpy writes the image sheared. Copy row
    by row at the plane's own stride instead.
    """
    buf = np.frombuffer(memoryview(plane), np.uint8)
    stride, row_bytes = plane.line_size, arr.shape[1] * arr.dtype.itemsize
    for y in range(arr.shape[0]):
        buf[y * stride: y * stride + row_bytes] = np.frombuffer(
            arr[y].tobytes(), np.uint8)


class HevcDepthEncoder(_FifoEncoder):
    """libx265 (PyAV): 10-bit disparity codes -> Annex-B HEVC **Main 10**, 1:1.

    Takes the luma codes ``quantize_disparity`` produces, in the LOW bits — this is
    the plain ``yuv420p10le`` convention. `NvHevcDepthEncoder` does NOT: see its
    docstring.
    """

    codec_name = "libx265"
    pix_fmt = DEPTH_PIX_FMT

    def __init__(self, width: int, height: int, *, keyint: int = 30,
                 crf: int = 6) -> None:
        super().__init__()
        from fractions import Fraction

        import av

        self._av = av
        self._w, self._h = width, height
        self._ctx = av.CodecContext.create("libx265", "w")
        self._ctx.width, self._ctx.height = width, height
        self._ctx.pix_fmt = DEPTH_PIX_FMT
        self._ctx.time_base = Fraction(1, 30)
        # Mirrors HevcEncoder's params for the same reasons (1:1, in order,
        # deterministic emission), plus repeat-headers: Foxglove requires VPS/SPS/PPS
        # in-band on every IRAP, and each AU here is its own CompressedVideo message
        # so it has to satisfy that alone.
        self._ctx.options = {
            "x265-params": (
                f"log-level=none:keyint={keyint}:min-keyint={keyint}:"
                "bframes=0:rc-lookahead=0:scenecut=0:frame-threads=1:"
                "repeat-headers=1"
            ),
            "crf": str(crf),
        }
        self._idx = 0

    def encode(self, codes: np.ndarray, t_ns: int) -> list[tuple[int, bytes]]:
        self._pending.append(int(t_ns))
        frame = self._av.VideoFrame(self._w, self._h, DEPTH_PIX_FMT)
        _fill_plane(frame.planes[0], np.ascontiguousarray(codes, np.uint16))
        for plane in frame.planes[1:]:
            np.frombuffer(memoryview(plane), np.uint16)[:] = _DEPTH_NEUTRAL_CHROMA
        frame.pts = self._idx
        self._idx += 1
        return self._pair(self._ctx.encode(frame), bytes)

    def flush(self) -> list[tuple[int, bytes]]:
        return self._pair(self._ctx.encode(None), bytes)


class NvHevcDepthEncoder(_FifoEncoder):
    """NVENC (PyNvVideoCodec): 10-bit disparity codes -> HEVC Main 10 via ``P010``.

    ⚠️ **P010 stores 10-bit data in the HIGH bits** — the sample is ``code << 6``,
    and neutral chroma is ``512 << 6 = 32768``. libx265's ``yuv420p10le`` takes the
    raw code in the LOW bits. Feeding one encoder's layout to the other yields depth
    wrong by a factor of 64 **with no error anywhere**, which is why the shift lives
    here, spelled once, rather than in a fill path shared with `HevcDepthEncoder`.

    Buffer layout is P010 semi-planar: a ``(h, w)`` uint16 luma plane followed by an
    interleaved ``(h/2, w)`` uint16 UV plane — but handed over as a **uint8 view**.
    Passing the uint16 array itself encodes BLACK FRAMES silently (measured: every
    sample decodes 0); PyNvVideoCodec reads the buffer's itemsize, and the ABGR path
    above hands it uint8 for the same reason. Both facts were established by probing
    a known constant through encode->decode, not from the docs.

    Lazy GPU dep, and raises on construction if NVENC cannot initialise, so
    `make_depth_encoder` can fall back exactly as `make_rect_encoder` does.
    """

    codec_name = "nvenc-hevc-p010"
    pix_fmt = "p010"
    _P010_SHIFT = 6

    def __init__(self, width: int, height: int, *, keyint: int = 30,
                 crf: int = 6, preset: str = "P3") -> None:
        super().__init__()
        import PyNvVideoCodec as nvc

        if height % 2:
            raise ValueError(f"P010 needs an even height, got {height}")
        self._enc = nvc.CreateEncoder(
            width, height, "P010", True,
            codec="hevc", preset=preset, tuning_info="ultra_low_latency",
            bf=0, gop=keyint, rc="constqp", qp=crf,
        )
        # `crf` is passed as NVENC's QP. The two scales are close but NOT identical,
        # so the two backends land at slightly different rate points for the same
        # number — deliberate: quality is the controlled variable, and the depth
        # sidecar's size is allowed to differ a little between them. (The rect-video
        # NVENC path does not equalise with libx265 either.)
        # Reused host scratch: luma rows then the interleaved UV rows.
        self._buf = np.empty((height + height // 2, width), np.uint16)
        self._buf[height:] = _DEPTH_NEUTRAL_CHROMA << self._P010_SHIFT
        self._h = height

    @staticmethod
    def _au(d) -> bytes:
        return bytes(d["data"])

    def encode(self, codes: np.ndarray, t_ns: int) -> list[tuple[int, bytes]]:
        self._pending.append(int(t_ns))
        self._buf[:self._h] = np.asarray(codes, np.uint16) << self._P010_SHIFT
        return self._pair(self._enc.Encode(self._buf.view(np.uint8)), self._au)

    def flush(self) -> list[tuple[int, bytes]]:
        return self._pair(self._enc.EndEncode(), self._au)


def make_depth_encoder(
    width: int, height: int, *, keyint: int, crf: int,
    choice: Literal["auto", "gpu", "cpu"], gpu_backend: bool,
    log: logging.Logger,
) -> HevcDepthEncoder | NvHevcDepthEncoder:
    """Pick the depth encoder — the exact shape of `make_rect_encoder`.

    ``"auto"`` => NVENC on the gpu backend, libx265 on cpu. A requested NVENC that
    cannot initialise warns and falls back, so the stage never hard-fails on a
    missing wheel or a capped NVENC session count (depth and rect video each take
    one).
    """
    want_gpu = choice == "gpu" or (choice == "auto" and gpu_backend)
    if want_gpu:
        try:
            return NvHevcDepthEncoder(width, height, keyint=keyint, crf=crf)
        except Exception as e:
            log.warning("NVENC unavailable (%s); depth video falls back to libx265", e)
    return HevcDepthEncoder(width, height, keyint=keyint, crf=crf)
