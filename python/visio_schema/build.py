"""Build foxglove payloads from plain values — the write-side counterpart of the
reader's adapters.

    read:   bytes → message → [adapter] → Element
    write:  values → [builder] → message → bytes

Each function takes numbers and returns a protobuf message. Nothing here writes,
opens a file, or knows what a topic is, so the same builder serves an MCAP write
and a live bus send::

    w.write(Message.stamped(pose_in_frame(t_ns, p, q, frame_id="odom"), t_ns),
            make_channel("/vio/pose", POSE_IN_FRAME, stream_id=16))

Deliberately NOT methods on a writer. A writer that knows what a depth map is has
to grow a method per payload type, and every consumer that wants to *send* one
rather than record it is stuck. Keeping the writer generic
(`McapWriter.write(msg, channel)`) and the type knowledge here means adding a
payload is a new function, not a new writer method.

Quaternions are ``(x, y, z, w)`` throughout — foxglove's order, and what
``scipy.spatial.transform.Rotation.as_quat()`` returns, so no reordering. (Some
in-house NPZs store ``wxyz``; do not carry that convention in here.)

Lives at the package root rather than under ``visio_schema/foxglove/``, which is
generated and gitignored — a hand-written module there would not survive
``make gen``.
"""

from __future__ import annotations

import numpy as np
from google.protobuf.message import Message as ProtoMessage

# The reader already names four of these; `_decode` owns the format table. Imported
# from the leaf modules, not the `reader` package, so `build` does not drag the
# session/decoder stack in behind it.
from visio_schema.reader._decode import decodable_formats
from visio_schema.reader.domain import (
    CAM_CALIB_SCHEMA as CAMERA_CALIBRATION,
)
from visio_schema.reader.domain import (
    FRAME_TF_SCHEMA as FRAME_TRANSFORM,
)
from visio_schema.reader.domain import (
    IMAGE_SCHEMA as COMPRESSED_IMAGE,
)
from visio_schema.reader.domain import (
    VIDEO_SCHEMA as COMPRESSED_VIDEO,
)
from visio_schema.wire.schema import message_class

__all__ = [
    "CAMERA_CALIBRATION",
    "COMPRESSED_IMAGE",
    "COMPRESSED_VIDEO",
    "FRAME_TRANSFORM",
    "POSE_IN_FRAME",
    "RAW_IMAGE",
    "camera_calibration",
    "compressed_image",
    "compressed_video",
    "frame_transform",
    "pose_in_frame",
    "raw_image_mono16",
]

# The two the reader has no name for; the other four are aliased from it above.
RAW_IMAGE = "foxglove.RawImage"
POSE_IN_FRAME = "foxglove.PoseInFrame"

# Resolved once. `decodable_formats` rebuilds its frozenset per call, and these are
# fixed for the life of the process.
#
# A STILL must be self-contained, so it is the mjpeg subset. A video stream is any
# format the decoder handles — motion-JPEG included, which is a legitimate video
# codec — so the video set is not the complement of the still set.
_STILL_FORMATS = decodable_formats("mjpeg")
_VIDEO_FORMATS = decodable_formats()


def _stamped(schema: str, t_ns: int) -> ProtoMessage:
    msg = message_class(schema)()
    msg.timestamp.FromNanoseconds(int(t_ns))
    return msg


def _require_decodable(who: str, fmt: str, allowed: frozenset[str]) -> None:
    """Refuse a format the reader cannot decode.

    The write side is the cheaper boundary: it knows at the first message, where
    the read side finds out on the first `next()` of a `stream()` an hour later.
    Applied to BOTH compressed builders — the failure mode is identical, and
    `HevcDecoder.__init__` raises for anything outside its table either way.
    """
    if fmt.lower() not in allowed:
        raise ValueError(
            f"{who}: {fmt!r} is not readable by this SDK "
            f"({', '.join(sorted(allowed))}). Output nothing here can decode is "
            f"not output this builder should produce."
        )


def raw_image_mono16(t_ns: int, image: np.ndarray, *, frame_id: str = "") -> ProtoMessage:
    """A single-channel uint16 image as ``foxglove.RawImage``.

    The encoding is the wire fact; what the numbers MEAN is the caller's (a depth
    stage writes millimetres, and says so on its topic). Little-endian ``<u2`` and
    ``step = width * 2`` are what the ``mono16`` encoding specifies.

    2-D only, enforced. `mono16` declares ``step = width * 2``, but the payload is
    whatever the array holds — so an ``(H, W, 3)`` array would declare one geometry
    and ship three times the bytes, and a consumer reading ``step * height`` decodes
    garbage from a message nothing rejected.
    """
    arr = np.asarray(image)
    if arr.ndim != 2:
        raise ValueError(
            f"raw_image_mono16: expected a 2-D single-channel image, got shape "
            f"{arr.shape}. mono16 declares step = width*2; a multi-channel array "
            f"would declare a geometry its own payload contradicts."
        )
    h, w = arr.shape
    msg = _stamped(RAW_IMAGE, t_ns)
    msg.frame_id = frame_id
    msg.width, msg.height = w, h
    msg.encoding = "mono16"
    msg.step = w * 2
    # One copy, not two: `ascontiguousarray(...).astype(...)` allocates twice.
    msg.data = np.ascontiguousarray(arr, dtype="<u2").tobytes()
    return msg


def compressed_video(
    t_ns: int, data: bytes, *, frame_id: str = "", fmt: str = "h265"
) -> ProtoMessage:
    """One access unit as ``foxglove.CompressedVideo`` (Annex-B, one frame per
    message, no B-frames — the CompressedVideo contract).

    ``fmt`` is refused unless the reader can decode it, for the same reason
    `compressed_image` refuses one: a sidecar written in a codec this SDK has no
    decoder for dies on the first read, an hour later, instead of here.
    """
    _require_decodable("compressed_video", fmt, _VIDEO_FORMATS)
    msg = _stamped(COMPRESSED_VIDEO, t_ns)
    msg.frame_id = frame_id
    msg.format = fmt
    msg.data = data
    return msg


def compressed_image(
    t_ns: int, data: bytes, *, frame_id: str = "", fmt: str = "jpeg"
) -> ProtoMessage:
    """One self-contained still as ``foxglove.CompressedImage``.

    The counterpart of `compressed_video` for pictures that are NOT a stream: each
    message decodes on its own, with no GOP and no predecessor. That is why it is
    a separate schema rather than a one-frame video — a consumer of the latter is
    entitled to open a video decoder and feed it every message in order, which is
    wrong for a stream sampled at 1 Hz out of a 30 fps recording.

    ``fmt`` is REFUSED unless the reader can decode it, checked against the
    decoder's own table. Writing a `png` here is otherwise accepted and then dies
    on the first `next()` of a `stream()` an hour later; the write side is the
    cheaper boundary because it knows at the first message.
    """
    _require_decodable("compressed_image", fmt, _STILL_FORMATS)
    msg = _stamped(COMPRESSED_IMAGE, t_ns)
    msg.frame_id = frame_id
    msg.format = fmt
    msg.data = data
    return msg


def pose_in_frame(
    t_ns: int, position, quat_xyzw, *, frame_id: str
) -> ProtoMessage:
    """A 6-DoF pose as ``foxglove.PoseInFrame``.

    Takes position + quaternion rather than a 4x4 so this layer needs no rotation
    library. ``frame_id`` is required, with no default: a pose is meaningless
    without the frame it is expressed in, and defaulting it silently publishes
    every pose in the wrong one.
    """
    msg = _stamped(POSE_IN_FRAME, t_ns)
    msg.frame_id = frame_id
    px, py, pz = (float(v) for v in np.asarray(position, float).reshape(3))
    msg.pose.position.x, msg.pose.position.y, msg.pose.position.z = px, py, pz
    qx, qy, qz, qw = (float(v) for v in np.asarray(quat_xyzw, float).reshape(4))
    (msg.pose.orientation.x, msg.pose.orientation.y,
     msg.pose.orientation.z, msg.pose.orientation.w) = qx, qy, qz, qw
    return msg


def frame_transform(
    t_ns: int,
    *,
    parent_frame_id: str,
    child_frame_id: str,
    translation_m,
    rotation_xyzw,
) -> ProtoMessage:
    """One TF edge as ``foxglove.FrameTransform``.

    The edge points parent -> child: it places ``child_frame_id``'s origin *within*
    ``parent_frame_id``, so ``p_parent = R @ p_child + t``. Foxglove resolves the
    tree by ``(parent, child)``, so exactly one producer may own a given edge — a
    second writes a silent conflict whose symptom is a flickering tree, not an
    error.
    """
    msg = _stamped(FRAME_TRANSFORM, t_ns)
    msg.parent_frame_id = parent_frame_id
    msg.child_frame_id = child_frame_id
    tx, ty, tz = np.asarray(translation_m, float).reshape(3)
    msg.translation.x, msg.translation.y, msg.translation.z = tx, ty, tz
    qx, qy, qz, qw = np.asarray(rotation_xyzw, float).reshape(4)
    msg.rotation.x, msg.rotation.y, msg.rotation.z, msg.rotation.w = qx, qy, qz, qw
    return msg


def camera_calibration(t_ns: int, calib) -> ProtoMessage:
    """A ``foxglove.CameraCalibration`` from the reader's `CameraCalib`.

    Round-trips what `Session.calibration` parsed, so a derived stream can publish
    the calibration its own images are in (e.g. a rectified K beside a rectified
    video) rather than leaving a consumer to guess it still matches the original.
    """
    msg = _stamped(CAMERA_CALIBRATION, t_ns)
    msg.frame_id = calib.frame_id
    msg.width, msg.height = calib.width, calib.height
    msg.distortion_model = calib.model
    msg.D.extend(np.asarray(calib.D, float).reshape(-1).tolist())
    msg.K.extend(np.asarray(calib.K, float).reshape(-1).tolist())
    if calib.R is not None:
        msg.R.extend(np.asarray(calib.R, float).reshape(-1).tolist())
    if calib.P is not None:
        msg.P.extend(np.asarray(calib.P, float).reshape(-1).tolist())
    return msg
