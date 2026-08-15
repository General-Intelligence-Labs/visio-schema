"""Gap-preserving H.265/H.264 access-unit decoder (alignment §3), and the
bitstream peek that tells a keyframe from a delta frame without decoding either.

One Visio ``CompressedVideo`` access unit (Annex-B, one frame per message)
decodes **1-in-1-out** to one frame, or ``None`` when it yields none — the
pre-keyframe warm-up, or a corrupt AU. The codecs here are all-P (no B-frames,
no reorder latency), so the frame an AU yields IS that AU's frame: the caller
stamps it with THAT message's capture timestamp, and a dropped AU takes its
timestamp with it (leave a gap, never shift later frames). An earlier FIFO that
re-indexed dropped AUs drifted a clip by ~7.7 s.

One decoder instance per camera stream — never feed two cameras through one.
Ported from ``slam-algo mcap_io._VideoDecoder`` / ``visio-data CameraDecoder``;
neither is importable from here, so the logic lives with the wire contract that
both of them read.
"""

from __future__ import annotations

import numpy as np

# Visio CompressedVideo.format -> PyAV codec name.
_AV = {
    "h265": "hevc",
    "hevc": "hevc",
    "h264": "h264",
    "avc": "h264",
    "av1": "av1",
    "mjpeg": "mjpeg",
    "jpeg": "mjpeg",
}

# How to read a NAL header's first byte, per codec: the type field's (shift, mask),
# then the VCL (slice) type range that ends the scan and the keyframe range inside
# it. Keyed on `_AV`'s decoder name rather than on the wire format, so the format
# aliases are spelled once, up there.
#   HEVC — type is bits 6..1; 0..31 are VCL, 16..21 (BLA_W_LP..CRA_NUT) are IRAP.
#   H.264 — type is the low 5 bits; 1..5 are VCL and only 5 is an IDR.
_NAL_SYNTAX = {
    "hevc": (1, 0x3F, 0, 31, 16, 21),
    "h264": (0, 0x1F, 1, 5, 5, 5),
}

_START_CODE = b"\x00\x00\x01"

def decodable_formats(codec: str | None = None) -> frozenset[str]:
    """Wire ``format`` strings this decoder accepts, optionally for one codec.

    Public because a WRITER has to agree with it: emitting a format the reader
    refuses produces a sidecar nothing can open. Derived from the decoder's own
    table rather than restated at the write end, so the two cannot drift —
    ``decodable_formats("mjpeg")`` is the self-contained-still subset a
    `foxglove.CompressedImage` may carry.
    """
    return frozenset(
        f for f, c in _AV.items() if codec is None or c == codec
    )


# Formats `is_keyframe` can classify — what a caller names when it has to explain
# the fallback, so the list never drifts from the tables above.
KEYFRAME_FORMATS = tuple(
    sorted(f for f, codec in _AV.items() if codec in _NAL_SYNTAX or codec == "mjpeg")
)

# How far into an AU to look for its first slice, and it is a REAL bound: it is
# passed to `find` as the end offset, not merely compared against, or a garbage AU
# would still be scanned to its last byte. Measured on real ego H.265 (3942 AUs):
# the first VCL NAL sits at offset 1 on a delta AU and 87 on a keyframe (VPS +
# SPS + PPS ahead of the slice), so 4 KB is ~47x the worst real prefix and still
# far short of the ~33 KB payload whose full scan costs 1.86 s/900 AUs — more than
# the decode this peek exists to skip.
_MAX_PREFIX_BYTES = 4096


def is_keyframe(fmt: str, access_unit: bytes) -> bool | None:
    """Is this AU independently decodable? ``None`` if the codec is not parsed here.

    The enabling fact for sparse sampling: an IRAP access unit carries its own
    VPS/SPS/PPS (the ``foxglove.CompressedVideo`` contract requires it), so feeding
    a decoder *only* these yields the exact same images a full forward decode
    would — verified byte-identical on real ego H.265, 30/30 frames — at 6.1x the
    speed, because the 29 P-frames between them are never reconstructed.

    ``foxglove.CompressedVideo`` has **no keyframe flag** — the schema is
    ``timestamp``/``frame_id``/``data``/``format`` — so this has to come off the
    bitstream. It reads the Annex-B NAL headers only as far as the **first VCL
    (slice) NAL**, which is what classifies the picture; everything after it is
    the payload we are trying not to touch. Emulation-prevention guarantees a
    ``00 00 01`` sequence never occurs inside a NAL, so scanning for start codes
    is exact rather than heuristic.

    ``False``, not ``None``, for a parsable codec whose AU holds no slice within
    the prefix bound: "can I feed this on its own?" has a definite answer there,
    and it is no. That answer is safe to give quietly only because a caller that
    finds NO keyframes in a whole file is expected to fail loudly — see
    ``Session._keyframe_frames`` — so a systematic misclassification surfaces as an
    error rather than as an empty result.

    ``None`` is reserved for a codec with no parser here (AV1), which is a property
    of the stream rather than of one message, so a caller checks it once and falls
    back instead of per AU.

    Related but NOT interchangeable: ``visio-setup``'s ``visio_stream._is_keyframe``
    walks the same NAL headers but answers a different question — it is True when a
    *parameter set* precedes the first slice. The two agree on today's ego, where
    the RV1106 repeats VPS/SPS/PPS at every IDR and nowhere else, and would diverge
    on a producer that repeated them ahead of a P slice.
    """
    codec = _AV.get(fmt.lower())
    if codec == "mjpeg":
        return True  # every JPEG is a complete picture
    syntax = _NAL_SYNTAX.get(codec)
    if syntax is None:
        return None
    # Resolved once, not per NAL: this loop is the whole reason the path is cheap.
    shift, mask, vcl_lo, vcl_hi, key_lo, key_hi = syntax
    limit = min(len(access_unit), _MAX_PREFIX_BYTES)
    i = access_unit.find(_START_CODE, 0, limit)
    while i != -1 and i + 3 < len(access_unit):
        nal = (access_unit[i + 3] >> shift) & mask
        if vcl_lo <= nal <= vcl_hi:  # the first slice classifies the picture
            return key_lo <= nal <= key_hi
        i = access_unit.find(_START_CODE, i + 3, limit)
    return False


# The PyAV this decoder's output was MEASURED against. A different one decodes a
# different H.265 frame set — same element counts, same topics, different pixels —
# so two runs under different PyAVs are silently incomparable. Kept in step with
# the dependency pin by `tests/reader/test_av_pin.py`, which is the only thing
# stopping this from becoming a second, drifting copy of the version.
# PyAV versions BELOW this all decode identically; 17.0.0 changed the output.
# Measured over 300 real ego H.265 access units: 12.3.0, 13.1.0, 14.x, 15.x and
# 16.x produce the same bytes, 17.0.0 and 18.0.0 produce different ones — and
# 16.1.0 and 17.0.0 ship the same libavcodec (62.11.100), so the change is PyAV's,
# not FFmpeg's. `pyproject.toml` caps the dependency here for that reason; its
# FLOOR (14.2) is a separate matter, set by an API the viewer needs.
AV_DECODE_CHANGED_AT = (17, 0, 0)
_av_checked = False


def _version_tuple(version: str) -> tuple[int, ...] | None:
    """Leading numeric components of a version string, or None if unreadable."""
    parts: list[int] = []
    for chunk in version.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or None


def _check_av_version(av) -> None:
    """Warn once if the installed PyAV decodes differently from the measured set.

    A warning, not a raise: a newer PyAV still decodes, and refusing would break
    every legitimate use that does not care about cross-run comparability. But it
    must not be SILENT — the determinism claim downstream ("N runs ->
    byte-identical MCAPs") is void the moment the decoder's output moves, and
    nothing else in the stack would ever say so.

    Scoped to the UPPER bound, because that is the one the output depends on. An
    older PyAV inside the compatible set is not worth a word.
    """
    global _av_checked
    if _av_checked:
        return
    _av_checked = True
    import logging

    log = logging.getLogger("visio_schema.reader")
    # No `getattr` default and no truthiness hedge: a PyAV that cannot report its
    # version is the ONE case where you genuinely do not know what you are running,
    # which is exactly what this exists to say out loud.
    installed = getattr(av, "__version__", "")
    parsed = _version_tuple(installed) if installed else None
    if parsed is None:
        log.warning(
            "Cannot determine the installed PyAV version; this decoder's output "
            "was measured below %s and is only comparable across runs within "
            "that set.",
            ".".join(str(p) for p in AV_DECODE_CHANGED_AT),
        )
    elif parsed >= AV_DECODE_CHANGED_AT:
        log.warning(
            "PyAV %s is installed, but this decoder's output was measured below "
            "%s — decoded pixels changed at that release, so output from this "
            "run is NOT comparable with output produced under an earlier one.",
            installed, ".".join(str(p) for p in AV_DECODE_CHANGED_AT),
        )


class HevcDecoder:
    """Decode one camera's AU stream to frames, 1-in-1-out."""

    def __init__(self, fmt: str, pixel_format: str = "rgb24") -> None:
        import av

        _check_av_version(av)
        codec = _AV.get(fmt.lower())
        if codec is None:
            raise ValueError(f"HevcDecoder: unsupported format {fmt!r}")
        self._av = av
        self._ctx = av.CodecContext.create(codec, "r")
        self._pixel_format = pixel_format
        self._synced = False
        self.hard_errors = 0

    def decode(self, access_unit: bytes) -> np.ndarray | None:
        """Decode one AU -> frame ndarray, or None (warm-up / corrupt AU).

        ``rgb24`` gives ``(H, W, 3)`` uint8; ``gray`` gives ``(H, W)`` uint8.
        """
        av = self._av
        try:
            frames = self._ctx.decode(av.Packet(access_unit))
        except av.error.FFmpegError:  # partial/corrupt NAL — heals at next IDR
            if self._synced:
                self.hard_errors += 1
            return None
        if not frames:  # need-more-data: pre-keyframe warm-up (benign)
            return None
        self._synced = True
        # All-P GOPs are strictly 1-in-1-out here, so frames[0] is THIS AU's.
        return frames[0].to_ndarray(format=self._pixel_format)
