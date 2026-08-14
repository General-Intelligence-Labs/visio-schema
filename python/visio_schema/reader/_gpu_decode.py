"""GPU H.265/H.264 access-unit decode to **device-resident RGB** frames.

The depth GPU fast-path's source stage. Where ``_decode.HevcDecoder`` decodes on
the CPU (PyAV → host ndarray), this decodes on NVDEC straight to an on-GPU RGB
surface, so a frame never leaves the device between decode → rectify → engine.

**Same emitted frames + byte-identical timestamps as the CPU path.** Validated on
real ego H.265 (RTX 5060): NVDEC output is bit-exact vs libav (mean|Δ| 0.18, the
RGB-conversion rounding), in PTS order. Both decoders drop the first ~7 frames of
a chunk while the reference/DPB primes, so the two produce the *same* frame set.

**Timestamp by PTS, not by call order.** NVDEC runs a deep pipeline (≈9 frames
in flight) and conceals corrupt AUs, so the frame a ``Decode`` call returns is
generally *not* the AU just fed. We therefore tag each AU's ``PacketData.pts``
with its capture ``t_ns`` (the MCAP ``log_time``; full int64 round-trips) and
read it back off the decoded frame — every frame self-identifies, immune to
pipeline delay and concealment. ``flush()`` drains the in-flight tail at each
chunk boundary (their PTS are correct too), so no trailing frames are lost.

**Surfaces are pool-recycled** — the ``DecodedFrame`` aliases an NVDEC surface
reused on the next ``Decode``, so each frame is copied out (device→device) into
its own cupy buffer before we move on.

Lazy GPU deps (``PyNvVideoCodec`` + ``cupy``): imported only when a decoder is
constructed, so the CPU-only SDK import path stays free of GPU wheels
(``visio-schema[gpu]``). One decoder per camera stream — never feed two through one.

Scope: this is a **camera** source (RGB). VIO's gray/bit-exact path stays on
PyAV; ``gray`` decode and IMU interleaving are not handled here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # device array; only a type hint, no import at module load
    import cupy

# Visio CompressedVideo.format -> PyNvVideoCodec cudaVideoCodec member name.
_CODEC = {
    "h265": "HEVC",
    "hevc": "HEVC",
    "h264": "H264",
    "avc": "H264",
    "av1": "AV1",
}

# One decoded frame, self-timestamped: (capture t_ns from PTS, device RGB
# (H, W, 3) uint8, optional CUDA event to wait on before reading the image).
DecodedFrame = tuple[int, "cupy.ndarray", "int | None"]


class NvDecoder:
    """Decode one camera's AU stream to device RGB frames, timestamped by PTS."""

    def __init__(self, fmt: str, *, gpuid: int = 0) -> None:
        import cupy
        import PyNvVideoCodec as nvc

        name = _CODEC.get(fmt.lower())
        if name is None:
            raise ValueError(f"NvDecoder: unsupported format {fmt!r}")
        self._nvc = nvc
        self._cupy = cupy
        self._memcpy = cupy.cuda.runtime.memcpy
        self._d2d = cupy.cuda.runtime.memcpyDeviceToDevice
        self._dec = nvc.CreateDecoder(
            gpuid=gpuid,
            codec=getattr(nvc.cudaVideoCodec, name),
            usedevicememory=True,
            outputColorType=nvc.OutputColorType.RGB,
        )
        # PacketData.bsl_data is a raw host pointer into this buffer; NVDEC reads
        # it during Decode, so it must outlive the call — hold the last one.
        self._au_buf: np.ndarray | None = None
        self.hard_errors = 0

    def decode(self, access_unit: bytes, t_ns: int) -> list[DecodedFrame]:
        """Feed one AU (tagged with ``t_ns``); return the frames it flushed out.

        Usually 0 (pipeline priming) or 1, occasionally more. Each carries the
        exact ``t_ns`` of the AU it decodes from, recovered from the frame PTS.
        """
        nvc = self._nvc
        self._au_buf = np.frombuffer(access_unit, np.uint8)
        pkt = nvc.PacketData()
        pkt.bsl = self._au_buf.size
        pkt.bsl_data = self._au_buf.ctypes.data
        pkt.pts = int(t_ns)
        return self._collect(self._dec.Decode(pkt))

    def flush(self) -> list[DecodedFrame]:
        """Drain the in-flight tail at end of stream (correct PTS preserved)."""
        out: list[DecodedFrame] = []
        nvc = self._nvc
        for _ in range(64):  # bounded; empty packet signals no-more-input
            pkt = nvc.PacketData()
            pkt.bsl = 0
            frames = self._dec.Decode(pkt)
            if not frames:
                break
            out.extend(self._collect(frames))
        return out

    def _collect(self, frames) -> list[DecodedFrame]:
        out: list[DecodedFrame] = []
        for fr in frames:
            # View aliasing the recycled NVDEC surface -> copy out (device->device)
            # into an owned contiguous buffer before the next Decode reuses it.
            view = self._cupy.from_dlpack(fr)
            buf = self._cupy.empty(view.shape, dtype=view.dtype)
            self._memcpy(buf.data.ptr, view.data.ptr, view.nbytes, self._d2d)
            out.append((int(fr.getPTS()), buf, None))
        return out
