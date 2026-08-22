"""Encode dataset videos: RGB frames -> an H.264 mp4, written incrementally.

The decode half lives in `visio_schema.reader` (`Session.stream` yields decoded
frames stamped with the message that produced each one); this is the symmetric
encode half, for writing a dataset's per-slot videos. Keeping both in one
package is what pins the ``av`` version range once — the decode determinism
bound documented in the package dependencies applies to this module's output
being re-read as much as to reading recordings.
"""

import contextlib
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from visio_schema.dataset.geometry import scaled_dims


@dataclass(frozen=True)
class VideoEncodeParams:
    """Encoder controls for the stored dataset videos.

    ``encoder`` defaults to ``libx264`` so a dataset's pixels do not depend on
    which machine converted it — a hardware encoder produces DIFFERENT output,
    and a silent auto-probe made that a property of the host rather than of the
    run. Set ``"h264_nvenc"`` to opt in, or ``"auto"`` to probe hardware and
    fall back. Any named encoder is probe-validated (fail loud if unusable).
    ``gop_size`` 1 = all-intra (fast random access for training loaders);
    0 = encoder default. ``quality`` maps to CRF (SW) / CQ (nvenc); 0 =
    encoder default. ``no_bframes`` disables B-frames (decode-order ==
    presentation-order).
    """

    encoder: str = "libx264"
    gop_size: int = 0
    quality: int = 0
    no_bframes: bool = False

    def options(self) -> dict[str, str]:
        opts: dict[str, str] = {}
        if self.gop_size:
            opts["g"] = str(self.gop_size)
        if self.no_bframes:
            opts["bf"] = "0"
        if self.quality:
            key = "cq" if "nvenc" in (self.encoder or "") else "crf"
            opts[key] = str(self.quality)
        return opts


_PROBED_ENCODER: str | None = None  # cache of the auto-probe result
_VALIDATED: set[str] = set()  # named encoders already proven (one probe each)


def _probe_encoder(requested: str) -> str:
    """Resolve the encoder name, validating with a real 1-frame encode.

    A named encoder must work (fail loud); ``"auto"`` tries hardware
    (h264_nvenc) and falls back to libx264 — a merely-registered HW encoder
    that cannot open a session (no GPU/driver) is rejected by the probe.
    """
    global _PROBED_ENCODER
    if requested and requested != "auto":
        if requested not in _VALIDATED:
            _validate_encoder(requested)
            _VALIDATED.add(requested)
        return requested
    if _PROBED_ENCODER is None:
        _PROBED_ENCODER = "libx264"
        for name in ("h264_nvenc",):
            try:
                _validate_encoder(name)
                _PROBED_ENCODER = name
                break
            except Exception:
                continue
    return _PROBED_ENCODER


def _validate_encoder(name: str) -> None:
    ctx = av.CodecContext.create(name, "w")
    ctx.width, ctx.height = 64, 64
    ctx.pix_fmt = "yuv420p"
    ctx.time_base = Fraction(1, 30)
    frame = av.VideoFrame.from_ndarray(
        np.zeros((64, 64, 3), dtype=np.uint8), format="rgb24"
    ).reformat(format="yuv420p")
    ctx.encode(frame)  # raises if the encoder cannot open a session


class Mp4Writer:
    """Encode RGB frames to an H.264 mp4, one frame at a time.

    A context manager rather than a function over a list, because a converter
    reads its recording as a *stream*: holding a whole episode's decoded frames
    to hand them over in one call is what made full-resolution capture
    impossible (a minute of 1080p30 across three cameras is ~33 GB). Written
    incrementally, the only frames resident are the ones `sync` is still holding
    for its lag budget.

    ``long_side`` caps the longer edge (aspect-preserving, even dims) — a
    converter passes its downscale cap so the stored video matches what a live
    pipeline downscales to before the model's letterbox. ``params`` selects the encoder
    + GOP/quality/B-frame controls.

    The container opens on the FIRST frame, not in ``__init__``: the output
    geometry is derived from the frame, and a writer that has been given nothing
    has no shape to declare. `close` on an unused writer therefore raises rather
    than leaving a zero-byte mp4 that a dataset would happily reference.
    """

    def __init__(
        self,
        path: str | Path,
        fps: int,
        *,
        long_side: int | None = None,
        params: VideoEncodeParams | None = None,
    ) -> None:
        self._path = Path(path)
        self._fps = fps
        self._long_side = long_side
        self._params = params or VideoEncodeParams(encoder="libx264")
        self._container = None
        self._stream = None
        self._out: tuple[int, int] | None = None  # (w, h)
        self._resize = False
        self._n = 0

    def __enter__(self) -> "Mp4Writer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
            return
        # Unwinding: actually drop the file. Closing alone writes the trailer,
        # so what is left is a VALID but short mp4 — which a dataset would
        # happily reference at a row count it does not match. Suppress any
        # teardown error so the caller's real exception is what propagates.
        with contextlib.suppress(Exception):
            if self._container is not None:
                self._container.close()
        self._container = None
        self._path.unlink(missing_ok=True)

    def write(self, frame: np.ndarray) -> None:
        """Encode one RGB ``(H, W, 3)`` uint8 frame."""
        if self._container is None:
            self._open(frame)
        vframe = av.VideoFrame.from_ndarray(frame, format="rgb24")
        if self._resize:
            vframe = vframe.reformat(width=self._out[0], height=self._out[1])
        self._container.mux(self._stream.encode(vframe))
        self._n += 1

    @property
    def shape(self) -> tuple[int, int, int]:
        """The stored ``(C, H, W)`` for ``info.json``, readable after closing.

        A property rather than `close`'s return value so a caller takes it AFTER
        its ``with`` block, and `close` runs exactly once. Reading it inside the
        block meant closing twice, which forced `close` to be idempotent — and
        any work later added there (an fsync, a sidecar) would have run twice.
        """
        if self._out is None:
            raise ValueError(
                f"{self._path}: no frames were written — refusing to report a "
                "shape for an mp4 that does not exist"
            )
        out_w, out_h = self._out
        return (3, out_h, out_w)

    def close(self) -> None:
        """Flush the encoder and close the container."""
        if self._container is None:
            return
        self._container.mux(self._stream.encode())  # flush
        self._container.close()
        self._container = None

    def _open(self, first: np.ndarray) -> None:
        encoder = _probe_encoder(self._params.encoder)
        height, width = first.shape[:2]
        out_w, out_h = scaled_dims(width, height, self._long_side)
        self._out = (out_w, out_h)
        self._resize = (out_w, out_h) != (width, height)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._container = av.open(str(self._path), mode="w")
        # PyAV wants a rational, not a float — and the grid rate legitimately
        # is one: 29.97 fps is 30000/1001 exactly, and rounding it to 30 makes
        # a stored video drift a frame every ~33 s against its own parquet
        # timestamps. The denominator cap is what turns the float back into
        # the ratio it came from rather than a 50-digit approximation.
        rate = Fraction(self._fps).limit_denominator(1001)
        self._stream = self._container.add_stream(encoder, rate=rate)
        self._stream.width, self._stream.height = out_w, out_h
        self._stream.pix_fmt = "yuv420p"
        options = self._params.options()
        if options:
            self._stream.codec_context.options = options


class SlotWriters:
    """One `Mp4Writer` per camera slot, each opened on that slot's first frame.

    A converter writes one video per slot from a single streaming pass, and
    cannot open the containers up front: the output geometry comes from the
    first frame, and a slot may legitimately contribute none at all (teleop
    drops every disengaged frame, so a camera can end an episode having written
    nothing). Opening lazily and reporting only the slots that produced frames
    keeps `info.json`'s video features honest.

    Writers are registered on the caller's `ExitStack`, so an exception anywhere
    in the pass unwinds every one of them — and `Mp4Writer.__exit__` drops its
    partial file rather than leaving a short mp4 a dataset would reference.
    """

    def __init__(self, stack, path_for, fps, *, long_side=None, params=None):
        self._stack = stack
        self._path_for = path_for
        self._fps = fps
        self._long_side = long_side
        self._params = params
        self._writers: dict[str, Mp4Writer] = {}

    def write(self, slot: str, image: np.ndarray) -> None:
        writer = self._writers.get(slot)
        if writer is None:
            writer = self._writers[slot] = self._stack.enter_context(
                Mp4Writer(
                    self._path_for(slot),
                    self._fps,
                    long_side=self._long_side,
                    params=self._params,
                )
            )
        writer.write(image)

    def shapes(self) -> dict[str, list[int]]:
        """``{slot: [C, H, W]}`` for the slots that produced frames.

        Valid only after the stack has closed the writers — the shape is a
        property of the finished file.
        """
        return {slot: list(w.shape) for slot, w in self._writers.items()}
