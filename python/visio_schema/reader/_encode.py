"""H.265 access-unit **encoder** — the write-side counterpart to ``_decode.py``.

Turns rectified RGB frames into per-frame Annex-B H.265 for a
``foxglove.CompressedVideo`` sidecar: **one frame per message, no B-frames**, so a
consumer decodes it the same 1-in-1-out way the recording's camera stream decodes.
Two backends behind one interface, picked by ``make_rect_encoder``:

- ``HevcEncoder``   — libx265 via PyAV (CPU): the reproducible reference, and the
  path the ``--backend cpu`` reference uses. Needs no GPU wheels.
- ``NvHevcEncoder`` — NVENC via ``PyNvVideoCodec`` (GPU): offloads the encode to the
  video engine so it never competes with the depth engine for CPU/GPU compute.

Both take a ``(H, W, 3)`` uint8 RGB frame — host or device — and return
``[(t_ns, au_bytes)]``. NVENC is handed a device NV12 surface we fill ourselves
(see :mod:`._gpu_color` for why the conversion is ours and not the driver's);
``NvHevcEncoder._INFLIGHT`` explains how long that buffer has to stay alive.


Counterpart of ``_decode``: the same codec table, and the x265 params here
(keyint, bframes=0, rc-lookahead=0, frame-threads=1) exist to emit exactly the
deterministic 1:1 Annex-B stream that decoder expects. It lives beside the
decoder rather than in a consumer so the two cannot drift — a second copy of
these params is a silent-corruption bug, and both this repo's tests and
visio-pp's need to synthesize a stream the reader will accept.
"""

from __future__ import annotations

import collections
import functools
import logging
from collections.abc import Callable
from typing import Literal

import numpy as np

# PyAV exposes `color_range` as a bare int with no named enum of its own.
_RANGE_LIMITED = 1  # AVCOL_RANGE_MPEG — 16-235
_RANGE_FULL = 2     # AVCOL_RANGE_JPEG — 0-255
# BT.709 happens to be 1 in all three of AVColorSpace, AVColorPrimaries and
# AVColorTransferCharacteristic, so one constant covers the whole description.
_BT709 = 1


def x265_params(keyint: int, *, repeat_headers: bool = False,
                frame_threads: int = 1, bitrate_kbps: int | None = None) -> str:
    """The libx265 settings every encoder here shares. One home, deliberately.

    keyint => periodic IDR (seekable); bframes=0 => no reorder (1:1, in order);
    rc-lookahead=0 => no lookahead delay.

    ``repeat_headers`` puts VPS/SPS/PPS on every IRAP rather than only the first.
    The rect video does not need it (a consumer reads the stream from its start);
    a per-frame `CompressedVideo` message does, because Foxglove requires each
    keyframe message to carry its own parameter sets.

    ``frame_threads`` defaults to **1**, and that is a latency choice rather than a
    quality one: one frame in, one packet out, deterministically. A live or
    reference path wants it, and `HevcDepthEncoder` MUST keep it — its luma is the
    disparity measurement, not a picture. An offline delivery encode wants the
    opposite and passes ``0`` (auto): measured 10.93 -> 17.31 fps at 1080p, with
    `bframes=0` and the GOP unchanged. The default lives here, not at the call
    sites, so adding this knob cannot silently re-tune depth.

    ``bitrate_kbps`` sets a one-pass ABR average with vbv capping the peak at 1.5x.
    ``None`` leaves libx265 on its default CRF — the reference-video behaviour.
    """
    rc = ""
    if bitrate_kbps:
        # `fps` rides WITH the rate control and only there: ABR has to know the frame
        # rate to hit an average, and x265 otherwise assumes 25. A CRF stream (depth)
        # needs none of it, and adding it there would re-tune a measurement stream for
        # no reason.
        peak = int(bitrate_kbps * 1.5)
        rc = (f":fps=30:bitrate={bitrate_kbps}"
              f":vbv-maxrate={peak}:vbv-bufsize={peak}")
    return (
        f"log-level=none:keyint={keyint}:min-keyint={keyint}:"
        f"bframes=0:rc-lookahead=0:scenecut=0:frame-threads={frame_threads}"
        + (":repeat-headers=1" if repeat_headers else "")
        + rc
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

    def __init__(self, width: int, height: int, *, keyint: int = 30,
                 bitrate_kbps: int | None = None, frame_threads: int = 0,
                 preset: str | None = "faster", full_range: bool = False) -> None:
        """``frame_threads``/``preset`` default to the DELIVERY point, not the
        reference one: this encoder's callers are offline. `HevcDepthEncoder` keeps
        `x265_params`' own `frame_threads=1` default and never sees a preset."""
        super().__init__()
        from fractions import Fraction

        import av

        self._ctx = av.CodecContext.create("libx265", "w")
        self._ctx.width, self._ctx.height = width, height
        self._ctx.pix_fmt = "yuv420p"
        self._ctx.time_base = Fraction(1, 30)
        self._ctx.framerate = Fraction(30, 1)  # VUI timing -> a raw-ES probe reads 30
        # Describe the picture the way it actually is, and CONVERT it that way.
        #
        # The tag alone is not enough: setting `color_range` only moves the VUI flag
        # while swscale keeps converting RGB -> YUV at LIMITED range, so the stream
        # shipped 16-235 samples labelled 0-255 and any decoder honouring the tag
        # expanded them a second time. Measured: RGB 0/255 -> Y 16/235 with the flag
        # on OR off. `encode` therefore hands the reformatter a full-range
        # DESTINATION; this attribute is only its label.
        #
        # The matrix travels with it. swscale's default here is BT.601 (pure red ->
        # Y 81) while the recorder signals BT.709 (`color_space=bt709`, red -> Y 54),
        # so a delivery that re-encodes a decoded 709 picture through the default
        # shifts every colour AND says nothing about it. Both are declared together
        # or not at all — a range without a matrix is still a stream a consumer has
        # to guess at.
        #
        # Defaults to False because this encoder is shared and flipping either half
        # moves the decoded pixel values every existing consumer sees (the fixture
        # that round-trips a frame index through them catches it). The DELIVERY asks
        # for this; nothing else has to care.
        self._ctx.color_range = _RANGE_FULL if full_range else _RANGE_LIMITED
        self._full_range = full_range
        self._reformat_kw: dict = {}
        if full_range:
            from av.video.reformatter import ColorRange, Colorspace

            self._ctx.colorspace = _BT709
            self._ctx.color_primaries = _BT709
            self._ctx.color_trc = _BT709
            self._reformat_kw = {"dst_color_range": ColorRange.JPEG,
                                 "dst_colorspace": Colorspace.ITU709}
        self._ctx.options = {
            "x265-params": x265_params(
                keyint, frame_threads=frame_threads, bitrate_kbps=bitrate_kbps),
        }
        if bitrate_kbps:
            # `bit_rate` on the context as well as in x265-params: PyAV reads it when
            # it opens the codec, and the two disagreeing is how a target silently
            # becomes advisory.
            self._ctx.bit_rate = bitrate_kbps * 1000
        if preset:
            # A SEPARATE option, never a key inside `x265-params`: x265's param
            # parser does not recognise `preset` there and ignores it silently
            # (measured: identical fps and a byte-identical stream). As an option it
            # reaches `x265_param_default_preset`, which FFmpeg applies BEFORE
            # parsing `x265-params` — so everything above still overrides it, and
            # `bframes=0`/`keyint` survive the faster rungs (verified).
            self._ctx.options["preset"] = preset
        self._idx = 0

    def encode(self, rgb: np.ndarray, t_ns: int) -> list[tuple[int, bytes]]:
        import av

        self._pending.append(int(t_ns))
        vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
        vf.color_range = _RANGE_FULL if self._full_range else _RANGE_LIMITED
        # `dst_color_range`/`dst_colorspace` are what actually steer swscale; the
        # frame's own attributes above describe the SOURCE, and for rgb24 they tell
        # it nothing it did not already know.
        vf = vf.reformat(format="yuv420p", **self._reformat_kw)
        vf.pts = self._idx  # monotonic counter so PyAV doesn't invent a pts
        self._idx += 1
        return self._pair(self._ctx.encode(vf), bytes)


def _want_gpu(choice: str, gpu_backend: bool) -> bool:
    """``"auto"`` follows the pipeline's backend; ``"gpu"``/``"cpu"`` override it."""
    return choice == "gpu" or (choice == "auto" and gpu_backend)


def _require_even(width: int, height: int, who: str) -> None:
    """4:2:0 has no half chroma sample, and neither arm degrades gracefully: x265
    refuses the geometry outright while the NV12 kernels truncate `W/2` and write a
    mis-sized buffer. Fail here so both fail the same way."""
    if width % 2 or height % 2:
        raise ValueError(f"{who}: 4:2:0 needs even dimensions, got {width}x{height}")


@functools.lru_cache(maxsize=8)
def insert_colour_vui(param_sets: bytes, width: int, height: int) -> bytes:
    """Add full-range BT.709 signalling to a blob of Annex-B parameter sets.

    Cached because it is pure and costs ~6 ms of PyAV setup, while `make_rect_encoder`
    runs once per CLIP per eye — a hundred-clip episode would otherwise spend a second
    of it rebuilding the same bytes.

    NVENC emits ``video_signal_type_present_flag = 0`` — no range, no matrix, no
    primaries — and PyNvVideoCodec exposes no VUI knob to change that (NVIDIA's API
    reference lists only codec/preset/tuning/rc/bitrate/gop/bf/profile/slice/timing,
    and candidate key names are accepted and silently ignored). So the fields have to
    be INSERTED, which reflows the rest of the SPS and its emulation prevention —
    FFmpeg's ``hevc_metadata`` filter already does that correctly, and hand-rolling a
    bitstream rewriter to avoid one dependency we already have would be a poor trade.
    """
    import io

    import av
    from av.bitstream import BitStreamFilterContext

    # The filter needs codec parameters to initialise, and the only thing that
    # carries them is a stream — so synthesize one over a throwaway buffer rather
    # than demux a file we do not have. Nothing is ever written to it.
    with av.open(io.BytesIO(), "w", format="hevc") as container:
        stream = container.add_stream("hevc")
        stream.width, stream.height = width, height
        bsf = BitStreamFilterContext(
            "hevc_metadata=video_full_range_flag=1:colour_primaries=1"
            ":transfer_characteristics=1:matrix_coefficients=1",
            in_stream=stream,
        )
        out = bsf.filter(av.Packet(param_sets))
        # `flush` returns None rather than an empty list when the filter is holding
        # nothing, which is the normal case here — parameter sets pass straight
        # through. Measured on PyAV 16.1; not a hedge.
        out += bsf.flush() or []
    patched = b"".join(bytes(p) for p in out)
    if not patched:
        raise RuntimeError(
            "insert_colour_vui: hevc_metadata returned no parameter sets; cannot "
            "declare the delivery's colour range")
    return patched


class NvHevcEncoder(_NvEncoder):
    """NVENC (PyNvVideoCodec): RGB -> Annex-B H.265 on the video engine.

    Takes a HOST ``(H, W, 3)`` uint8 RGB frame or a DEVICE one (cupy / anything with
    ``__cuda_array_interface__``) — a host frame is uploaded once and everything after
    that stays on the GPU, so a device-resident pipeline never round-trips through
    system memory. See :mod:`._gpu_color` for why the conversion to NV12 is ours and
    not the driver's.

    Lazy GPU deps (``PyNvVideoCodec``, ``cupy``, ``cvcuda``): imported only when
    constructed, so the CPU-only SDK import path stays free of GPU wheels. Raises on
    construction if NVENC can't initialise (missing wheel, no encode-capable GPU,
    session cap) — ``make_rect_encoder`` catches that and falls back to libx265.
    """

    codec_name = "nvenc-hevc"

    # NVENC runs a ~3-frame asynchronous pipeline and reads the input surface after
    # `Encode` returns, so a buffer recycled too early is read mid-encode. Hold a few
    # more than the pipeline depth and rotate.
    _INFLIGHT = 6

    def __init__(self, width: int, height: int, *, keyint: int = 30,
                 preset: str = "P3", bitrate_kbps: int | None = None,
                 full_range: bool = False) -> None:
        super().__init__()
        import cupy
        import cvcuda
        import PyNvVideoCodec as nvc

        self._cupy, self._cvcuda = cupy, cvcuda
        self._full_range = full_range
        # A delivery target (bitrate_kbps) => VBR at that average, vbv peak 1.5x, and
        # the high_quality tuning; None keeps the low-latency reference-video default.
        if bitrate_kbps:
            rc = dict(tuning_info="high_quality", rc="vbr",
                      bitrate=bitrate_kbps * 1000, maxbitrate=int(bitrate_kbps * 1500))
        else:
            rc = dict(tuning_info="ultra_low_latency")
        # Bind NVENC to the CUDA context and stream cupy is already using. Without
        # this it makes its own context, and a device pointer from ours is then not
        # one it can read. The DEVICE is whichever one the caller already selected —
        # `cupy.empty` forces that one's primary context to exist without moving it.
        cupy.empty(1, cupy.uint8)
        ctx = cupy.cuda.driver.ctxGetCurrent()
        stream = cupy.cuda.get_current_stream().ptr
        # `usecpuinputbuffer=False` + NV12: we hand NVENC a device surface we filled
        # ourselves. NOTE the input must be an NVCV (cvcuda) tensor — a bare cupy
        # array is rejected with "incorrect usage of CPU input buffer" even here,
        # so `encode` wraps every buffer with `cvcuda.as_tensor` (zero-copy).
        self._enc = nvc.CreateEncoder(
            width, height, "NV12", False, cudacontext=ctx, cudastream=stream,
            codec="hevc", preset=preset, bf=0, gop=keyint, **rc,
        )
        self._shape = (height, width)
        self._pool: collections.deque = collections.deque(maxlen=self._INFLIGHT)
        self._swapped = 0
        self._params = self._colour_params(width, height) if full_range else None

    def _colour_params(self, width: int, height: int) -> tuple[bytes, bytes]:
        """``(as NVENC writes them, with the colour description added)``.

        Runs ONCE, because those bytes are constant for an encoder session: NVENC
        prefixes every IRAP access unit with exactly this blob, so `_au` only has to
        swap a known prefix (measured 0.5 us/AU, against 0.3 ms/AU to filter each).
        """
        original = bytes(self._enc.GetSequenceParams())
        return original, insert_colour_vui(original, width, height)

    def encode(self, rgb, t_ns: int) -> list[tuple[int, bytes]]:
        from ._gpu_color import rgb_to_nv12

        if rgb.shape[:2] != self._shape:
            # NVENC is configured for one geometry; a frame of another size would be
            # converted into a correctly-shaped NV12 buffer of the WRONG picture.
            raise ValueError(
                f"NvHevcEncoder: frame is {rgb.shape[1]}x{rgb.shape[0]} but the "
                f"session was opened at {self._shape[1]}x{self._shape[0]}")
        self._pending.append(int(t_ns))
        src = rgb if hasattr(rgb, "__cuda_array_interface__") else self._cupy.asarray(rgb)
        nv12 = rgb_to_nv12(src, full_range=self._full_range)
        self._pool.append(nv12)  # keep it alive while NVENC is still reading it
        packets = self._enc.Encode(self._cvcuda.as_tensor(nv12[:, :, None], "HWC"))
        return self._pair(packets, self._au)

    def _au(self, d) -> bytes:
        """Every emitted access unit passes here, on both `encode` and `flush`.

        Overriding the pairing hook rather than wrapping two call sites is what
        stops the two paths drifting — `flush`'s tail would otherwise be the one
        that quietly shipped unpatched parameter sets.
        """
        au = bytes(d["data"])
        if self._params is None:
            return au
        original, patched = self._params
        if not au.startswith(original):
            return au  # a delta frame: it carries no parameter sets to swap
        self._swapped += 1
        return patched + au[len(original):]

    def flush(self) -> list[tuple[int, bytes]]:
        out = super().flush()
        if self._params is not None and not self._swapped:
            # Every IRAP is supposed to carry the blob `_colour_params` probed. If
            # none ever matched, the driver emits something else per-AU and the whole
            # stream shipped with no colour signalling — silently, which is the one
            # outcome this feature exists to prevent.
            raise RuntimeError(
                "NvHevcEncoder: no access unit carried the parameter sets this "
                "session probed, so the delivery has no colour signalling")
        return out


def make_rect_encoder(
    width: int, height: int, *, keyint: int,
    choice: Literal["auto", "gpu", "cpu"], gpu_backend: bool,
    log: logging.Logger, bitrate_kbps: int | None = None,
    frame_threads: int = 0, preset: str | None = "faster",
    full_range: bool = False,
) -> HevcEncoder | NvHevcEncoder:
    """Pick the rect-video H.265 encoder.

    ``choice`` is ``"auto" | "gpu" | "cpu"``: ``"auto"`` => NVENC on the gpu backend,
    libx265 on the cpu backend; ``"gpu"`` forces NVENC; ``"cpu"`` forces libx265. If
    NVENC is requested but cannot initialise, warn and fall back to libx265, so the
    stage never hard-fails on a missing/capped NVENC session.

    ``bitrate_kbps`` sets a one-pass ABR/VBR average (a delivery bitrate floor) on
    whichever encoder is chosen; ``None`` leaves each at its default (CRF / low
    latency) — the reference-video behaviour a depth run wants.

    ``frame_threads``/``preset`` tune the libx265 arm only. They are x265's own
    vocabulary and NVENC's ``preset`` is a different namespace ("P1".."P7"), so
    forwarding one to the other would be a category error, not a convenience.

    ``full_range`` is different: it describes the STREAM, not the encoder, so a
    caller that asks for it means it whichever encoder runs — full-range BT.709
    samples under a VUI that says so, on both backends. Neither encoder gets there
    by itself (swscale converts at limited range whatever the tag says; NVENC's
    packed-RGB path is hardwired to BT.470BG limited and writes no VUI at all), so
    each arm builds it deliberately. A delivery whose colour depends on which
    machine encoded it is the bug this argument exists to prevent.
    """
    _require_even(width, height, "make_rect_encoder")
    if _want_gpu(choice, gpu_backend):
        try:
            return NvHevcEncoder(width, height, keyint=keyint,
                                 bitrate_kbps=bitrate_kbps, full_range=full_range)
        except Exception as e:
            log.warning("NVENC unavailable (%s); rect video falls back to libx265", e)
    # The fallback carries the delivery settings too: an NVENC miss must change the
    # encoder, never the rate target the caller asked for.
    return HevcEncoder(width, height, keyint=keyint, bitrate_kbps=bitrate_kbps,
                       frame_threads=frame_threads, preset=preset,
                       full_range=full_range)


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
    # Checked before the try below: 4:2:0 needs even dimensions on BOTH encoders, so
    # demoting this to "NVENC unavailable" would fall back to a path that cannot take
    # them either, and send the reader hunting for a GPU problem that does not exist.
    _require_even(width, height, "make_depth_encoder")
    if _want_gpu(choice, gpu_backend):
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
