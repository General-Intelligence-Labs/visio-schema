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


def x265_params(keyint: int, *, repeat_headers: bool = False) -> str:
    """The libx265 settings every encoder here shares. One home, deliberately.

    keyint => periodic IDR (seekable); bframes=0 => no reorder (1:1, in order);
    rc-lookahead=0 + frame-threads=1 => low-latency, deterministic emission.

    ``repeat_headers`` puts VPS/SPS/PPS on every IRAP rather than only the first.
    The rect video does not need it (a consumer reads the stream from its start);
    a per-frame `CompressedVideo` message does, because Foxglove requires each
    keyframe message to carry its own parameter sets.
    """
    return (
        f"log-level=none:keyint={keyint}:min-keyint={keyint}:"
        "bframes=0:rc-lookahead=0:scenecut=0:frame-threads=1"
        + (":repeat-headers=1" if repeat_headers else "")
    )


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


class _PyAvEncoder(_FifoEncoder):
    """A libx265 codec context: `flush` drains it, subclasses build the frame."""

    def flush(self) -> list[tuple[int, bytes]]:
        return self._pair(self._ctx.encode(None), bytes)


class _NvEncoder(_FifoEncoder):
    """An NVENC session: `flush` ends it, subclasses build the input buffer."""

    @staticmethod
    def _au(d) -> bytes:
        return bytes(d["data"])

    def flush(self) -> list[tuple[int, bytes]]:
        return self._pair(self._enc.EndEncode(), self._au)


class HevcEncoder(_PyAvEncoder):
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
        self._ctx.options = {"x265-params": x265_params(keyint)}
        self._idx = 0

    def encode(self, rgb: np.ndarray, t_ns: int) -> list[tuple[int, bytes]]:
        import av

        self._pending.append(int(t_ns))
        vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
        vf = vf.reformat(format="yuv420p")
        vf.pts = self._idx  # monotonic counter so PyAV doesn't invent a pts
        self._idx += 1
        return self._pair(self._ctx.encode(vf), bytes)


class NvHevcEncoder(_NvEncoder):
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

    def encode(self, rgb: np.ndarray, t_ns: int) -> list[tuple[int, bytes]]:
        self._pending.append(int(t_ns))
        self._abgr[..., :3] = rgb  # host RGB -> ABGR bytes [R, G, B, 255]
        return self._pair(self._enc.Encode(self._abgr), self._au)


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


def quantize_disparity(disparity: np.ndarray) -> np.ndarray:
    """Disparity px -> the 10-bit luma codes a depth stream carries.

    One home for the grid, so the encoders, the stage's verify harness and any
    consumer computing ``depth_scale`` cannot disagree about it.

    The two ends are NOT symmetric, deliberately.

    Above the ceiling it raises rather than clipping. This is a WRITER: saturation
    would bake a flattened near field into a permanent artifact, and nothing
    downstream can tell a real 1023 from a clipped one. With the shipped
    `max_disp=192` and this grid, `192 * 4 = 768 < 1023`, so the condition means
    the engine and the grid disagree — a misconfiguration, not a data value.

    Below zero it clamps to 0, because that IS a data value: a matcher marks an
    unmatched pixel with non-positive disparity, and 0 is already this format's
    "no depth" code (`disparity_to_depth_mm` guards `disp > 0` the same way).
    Without the clamp the uint16 cast wraps -0.5 px to 65534, turning an invalid
    pixel into a bogus NEAR reading — silent, and unrecoverable once written.
    """
    q = np.round(np.asarray(disparity, np.float32) * DEPTH_DISPARITY_FRAC)
    peak = float(np.nanmax(q, initial=0.0))
    if peak > _DEPTH_MAX_CODE:
        raise ValueError(
            f"disparity {peak / DEPTH_DISPARITY_FRAC:.1f} px exceeds what the "
            f"1/{DEPTH_DISPARITY_FRAC:g}-px {DEPTH_BITS}-bit grid can carry "
            f"({_DEPTH_MAX_CODE / DEPTH_DISPARITY_FRAC:.1f} px) — the engine's "
            f"max_disp and this grid disagree"
        )
    # NaN maps to 0 here too (an unmatched pixel by another name); relying on the
    # cast to do it would be relying on x86's out-of-range float->int convention.
    return np.nan_to_num(np.clip(q, 0.0, None), nan=0.0).astype(np.uint16)


def dequantize_disparity(codes: np.ndarray) -> np.ndarray:
    """The inverse of `quantize_disparity`: luma codes -> disparity px.

    Exists so no consumer spells `/ DEPTH_DISPARITY_FRAC` itself; the grid is one
    fact and it changes in one place.
    """
    return np.asarray(codes, np.float32) / DEPTH_DISPARITY_FRAC


def _fill_plane(plane, arr: np.ndarray) -> None:
    """Copy a ``(h, w)`` array into an ``av`` plane, honouring its ``line_size``.

    Planes carry row padding, so a flat memcpy writes the image sheared. Reshaping
    the raw buffer to ``(h, stride)`` and assigning into the left ``row_bytes``
    columns lands every row at the right offset in one vectorized write — 0.63 ms
    per 960x544 frame as a Python row loop, 0.034 ms this way.
    """
    h, row_bytes = arr.shape[0], arr.shape[1] * arr.dtype.itemsize
    stride = plane.line_size
    buf = np.frombuffer(memoryview(plane), np.uint8)
    buf[:h * stride].reshape(h, stride)[:, :row_bytes] = arr.view(np.uint8).reshape(
        h, row_bytes)


class HevcDepthEncoder(_PyAvEncoder):
    """libx265 (PyAV): 10-bit disparity codes -> Annex-B HEVC **Main 10**, 1:1.

    Takes the luma codes ``quantize_disparity`` produces, in the LOW bits — this is
    the plain ``yuv420p10le`` convention. `NvHevcDepthEncoder` does NOT: see its
    docstring.
    """

    codec_name = "libx265"

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
            "x265-params": x265_params(keyint, repeat_headers=True),
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


class NvHevcDepthEncoder(_NvEncoder):
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

    ⚠️ **`crf` does not reach NVENC**, and that is a property of the binding rather
    than a choice. Measured on real disparity: passing `rc="constqp"` collapses
    quality to 1.34 px p95 REGARDLESS of `qp` (0, 6, 12 and 24 all produce
    byte-identical output), so the QP is not merely ignored — asking for it is
    actively harmful. `tuning_info="ultra_low_latency"`, inherited from the
    rect-video encoder where latency is the point, does the same thing.

    So this runs at one quality point — `preset="P7", tuning_info="high_quality"` —
    chosen because it lands within 6% of the libx265 default:

        libx265 crf 6            21.85 KiB/frame   0.347 px p95
        NVENC P7 high_quality    14.10 KiB/frame   0.368 px p95

    `make_depth_encoder` warns when a caller asks for a `crf` this cannot honour.
    Use `choice="cpu"` when the exact rate point matters more than throughput — but
    note libx265 costs 2.7x on the depth stage (16.7 vs 45.7 pairs/s measured),
    which is the whole reason this encoder exists.

    Lazy GPU dep, and raises on construction if NVENC cannot initialise, so
    `make_depth_encoder` can fall back exactly as `make_rect_encoder` does.
    """

    codec_name = "nvenc-hevc-p010"
    _P010_SHIFT = 6
    #: The libx265 `crf` this encoder's fixed quality point is equivalent to.
    EQUIVALENT_CRF = 6

    def __init__(self, width: int, height: int, *, keyint: int = 30) -> None:
        super().__init__()
        import PyNvVideoCodec as nvc

        # No `rc`/`qp`: see the class docstring — passing them costs 4x the error.
        self._enc = nvc.CreateEncoder(
            width, height, "P010", True,
            codec="hevc", preset="P7", tuning_info="high_quality",
            bf=0, gop=keyint,
        )
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

    NVENC has ONE quality point (see `NvHevcDepthEncoder`), equivalent to libx265
    crf 6. A caller asking for anything else is told rather than silently given
    something different — the failure mode this guards against is a run configured
    for near-lossless depth that quietly ships the default instead.
    """
    if width % 2 or height % 2:
        # Checked here, not inside the try below: 4:2:0 needs even dimensions on
        # BOTH encoders, so demoting this to "NVENC unavailable" would fall back to
        # a path that cannot take them either, and send the reader hunting for a GPU
        # problem that does not exist.
        raise ValueError(
            f"4:2:0 needs even dimensions, got {width}x{height}")
    want_gpu = choice == "gpu" or (choice == "auto" and gpu_backend)
    if want_gpu:
        try:
            encoder = NvHevcDepthEncoder(width, height, keyint=keyint)
        except Exception as e:
            log.warning("NVENC unavailable (%s); depth video falls back to libx265", e)
        else:
            # Asked of the encoder that was actually built, not of the module
            # global: what quality point applies is a property of the instance.
            if crf != encoder.EQUIVALENT_CRF:
                log.warning(
                    "depth_crf=%d cannot be honoured by NVENC, which has one quality "
                    "point (~crf %d); it is being IGNORED. Use "
                    "--depth_video_encoder cpu to get the rate point you asked for, "
                    "at ~2.7x the stage's wall clock.",
                    crf, encoder.EQUIVALENT_CRF,
                )
            return encoder
    return HevcDepthEncoder(width, height, keyint=keyint, crf=crf)
