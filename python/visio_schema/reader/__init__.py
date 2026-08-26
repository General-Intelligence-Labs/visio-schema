"""The element reader — rows on one timeline, aligned.

`visio_schema.mcap.read_mcap` and `visio_schema.stream.read_serial` are the two
**row** sources in this package, and a Visio bus sink pops the same pairs. This
package sits directly on top of them and turns rows into **elements** — decoded,
unbundled, clock-normalized values that share one interface:

    rows       (Message, Channel)          read_mcap / read_serial / a bus sink
    elements   Frame | ImuSample | Record  Session.stream, elements  ← here

That is a real transformation, not a rename. A `Frame` carries a decoded array
rather than an H.265 access unit; one `ImuRaw` bundle expands to ~200
`ImuSample`s, each on its own clock; and every element exposes ``topic`` and
``t_ns`` so `sync` can align them without knowing what they are.

**Two entry points, one decoder.** `Session` owns a recording — indexed metadata,
topic-filtered chunk reads, several files merged, the device topic prefix stripped.
`elements` owns a row *stream* and does none of that, which is what a live loop
needs; it is also how that loop replays its own recording, so live and offline
produce the same elements and can be compared group for group.

**The clock.** ``Element.t_ns`` IS the wire stamp — the heartbeat-synchronized
``Header.timestamp``, which the MCAP stores as ``log_time``. No sensor-latency or
exposure correction is applied here; this layer's job is to *inform* about
timing, not to decide it. Unbundling an `ImuRaw` into per-sample times is
decoding the wire format, not correcting a clock, so that stays.

Not exported from the `visio_schema` facade: the facade is frozen by
``tests/test_public_api.py`` and needs a MAJOR bump to change, while this surface
is still settling. Import it explicitly::

    from visio_schema.reader import Session, sync, resample

Needs the ``[reader]`` extra (numpy, scipy) plus the base ``av`` and ``mcap``.
"""

from __future__ import annotations

from ._decode import (
    KEYFRAME_FORMATS,
    RAW_LUMA16,
    HevcDecoder,
    decodable_formats,
    is_keyframe,
)
from ._encode import (
    DEPTH_DISPARITY_FRAC,
    DEPTH_PIX_FMT,
    HevcDepthEncoder,
    HevcEncoder,
    NvHevcDepthEncoder,
    NvHevcEncoder,
    dequantize_disparity,
    make_depth_encoder,
    make_rect_encoder,
    quantize_disparity,
)
from .adapters import (
    AdapterContext,
    AdapterFactory,
    ElementAdapter,
    build_adapter,
    element_adapter,
    registered_schemas,
)
from .domain import (
    CAM_CALIB_SCHEMA,
    FRAME_INFO_SCHEMA,
    FRAME_TF_SCHEMA,
    IMAGE_SCHEMA,
    IMU_CALIB_SCHEMA,
    IMU_RAW_SCHEMA,
    JOINT_STATES_SCHEMA,
    POSE_SCHEMA,
    VIDEO_SCHEMA,
    Calibration,
    CameraCalib,
    Element,
    Frame,
    FrameExposure,
    ImuSample,
    JointState,
    KeyframeCadence,
    Ns,
    Pose,
    Record,
    Sampled,
    SampleMethod,
    SessionMeta,
    SyncGroup,
    Tick,
    TopicInfo,
    make_T,
)
from .interp import blend, interpolator, slerp_xyzw
from .ops import ResampleMode, prefetch, sync
from .rows import cpu_video_decoders, elements, resolve_message_class
from .session import Session, strip_device_topic_prefix

__all__ = [
    "CAM_CALIB_SCHEMA",
    "DEPTH_DISPARITY_FRAC",
    "DEPTH_PIX_FMT",
    "FRAME_INFO_SCHEMA",
    "FRAME_TF_SCHEMA",
    "IMAGE_SCHEMA",
    "IMU_CALIB_SCHEMA",
    "IMU_RAW_SCHEMA",
    "JOINT_STATES_SCHEMA",
    "KEYFRAME_FORMATS",
    "POSE_SCHEMA",
    "RAW_LUMA16",
    "VIDEO_SCHEMA",
    "AdapterContext",
    "AdapterFactory",
    "Calibration",
    "CameraCalib",
    "Element",
    "ElementAdapter",
    "Frame",
    "FrameExposure",
    "HevcDecoder",
    "HevcDepthEncoder",
    "HevcEncoder",
    "ImuSample",
    "JointState",
    "KeyframeCadence",
    "Ns",
    "NvHevcDepthEncoder",
    "NvHevcEncoder",
    "Pose",
    "Record",
    "ResampleMode",
    "SampleMethod",
    "Sampled",
    "Session",
    "SessionMeta",
    "SyncGroup",
    "Tick",
    "TopicInfo",
    "blend",
    "build_adapter",
    "cpu_video_decoders",
    "decodable_formats",
    "dequantize_disparity",
    "element_adapter",
    "elements",
    "interpolator",
    "is_keyframe",
    "make_T",
    "make_depth_encoder",
    "make_rect_encoder",
    "prefetch",
    "quantize_disparity",
    "registered_schemas",
    "resolve_message_class",
    "slerp_xyzw",
    "strip_device_topic_prefix",
    "sync",
]
