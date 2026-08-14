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
