"""The element reader — rows on one timeline, aligned.

`visio_schema.mcap.read_mcap` and `visio_schema.stream.read_serial` are the two
**row** sources: both yield ``(Message, Channel)``. This package sits directly on
top of them and turns rows into **elements** — decoded, unbundled,
clock-normalized values that share one interface:

    rows       (Message, Channel)          read_mcap / read_serial
    elements   Frame | ImuSample | Record  Session.stream  ← here

That is a real transformation, not a rename. A `Frame` carries a decoded array
rather than an H.265 access unit; one `ImuRaw` bundle expands to ~200
`ImuSample`s, each on its own clock; the device topic prefix is stripped; and
every element exposes ``topic`` and ``t_ns`` so `sync` and `resample` can align
them without knowing what they are.

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

from ._decode import KEYFRAME_FORMATS, HevcDecoder, decodable_formats, is_keyframe
from ._encode import HevcEncoder, NvHevcEncoder, make_rect_encoder
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
    VIDEO_SCHEMA,
    Calibration,
    CameraCalib,
    Element,
    Frame,
    FrameExposure,
    ImuSample,
    KeyframeCadence,
    Ns,
    Record,
    SessionMeta,
    SyncGroup,
    TopicInfo,
    make_T,
)
from .ops import prefetch, resample, sync
from .session import Session, strip_device_topic_prefix

__all__ = [
    "CAM_CALIB_SCHEMA",
    "FRAME_INFO_SCHEMA",
    "FRAME_TF_SCHEMA",
    "IMAGE_SCHEMA",
    "IMU_CALIB_SCHEMA",
    "IMU_RAW_SCHEMA",
    "KEYFRAME_FORMATS",
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
    "HevcEncoder",
    "ImuSample",
    "KeyframeCadence",
    "Ns",
    "NvHevcEncoder",
    "Record",
    "Session",
    "SessionMeta",
    "SyncGroup",
    "TopicInfo",
    "build_adapter",
    "decodable_formats",
    "element_adapter",
    "is_keyframe",
    "make_T",
    "make_rect_encoder",
    "prefetch",
    "registered_schemas",
    "resample",
    "strip_device_topic_prefix",
    "sync",
]
