"""StreamKind → payload-type reflection, driven by EnumValueOptions on
`visio_schema.wire.v1.StreamKind` per visio-schema/docs/stream_type_map.md.

The proto file is the source of truth. We read the `(visio_proto_type)`
and `(visio_mcap_schema_name)` extensions off each enum value's
descriptor — no codegen, no markdown parsing. This is pure schema
reflection (no transport), shipped inside the `visio-schema` package so
MCAP writers, Foxglove bridges, and the bus can all share one mapping.

Dual-payload service streams (STREAM_TIMESYNC, STREAM_DEVICE_INFO) are
deliberately UNannotated in the proto; `for_kind()` returns None for them.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from typing import Iterator

from google.protobuf import descriptor_pb2, symbol_database
from google.protobuf.descriptor import FileDescriptor
from google.protobuf.message import Message as _ProtoMessage

from visio_schema.wire.v1 import header_pb2 as _hdr

# Generated payload modules whose types StreamKind annotations reference.
# Importing them registers their descriptors in the default pool so
# `message_class()` / `file_descriptor_set()` can resolve any annotated
# StreamKind. Mirrors visio-schema/tests/test_imports.py; extend both
# when a new payload type is added.
_PAYLOAD_MODULES = (
    "visio_schema.sensor.v1.imu_raw_pb2",
    "visio_schema.sensor.v1.encoder_raw_pb2",
    "visio_schema.sensor.v1.system_health_pb2",
    "visio_schema.sensor.v1.audio_compressed_pb2",
    "visio_schema.sensor.v1.button_pb2",
    "visio_schema.calibration.v1.imu_pb2",
    "visio_schema.ros.geometry_msgs.v1.quaternion_pb2",
    "visio_schema.input.v1.quest_controller_state_pb2",
    "visio_schema.geometry.v1.twist_pb2",
    "visio_schema.control.v1.command_pb2",
    "visio_schema.service.device_info.v1.device_info_pb2",
    "visio_schema.service.heartbeat.v1.heartbeat_pb2",
    "visio_schema.foxglove.CompressedVideo_pb2",
    "visio_schema.foxglove.CompressedImage_pb2",
    "visio_schema.foxglove.RawImage_pb2",
    "visio_schema.foxglove.CameraCalibration_pb2",
    "visio_schema.foxglove.RawAudio_pb2",
    "visio_schema.foxglove.PoseInFrame_pb2",
    "visio_schema.foxglove.FrameTransforms_pb2",
    "visio_schema.foxglove.JointStates_pb2",
    "visio_schema.foxglove.Log_pb2",
)


def _load_payload_modules() -> None:
    # Import eagerly and let ImportError propagate: a missing module means
    # codegen is incomplete (run `make gen`). Swallowing it would leave the
    # registry silently half-populated and only fail much later, at the first
    # MCAP write or Foxglove publish of the missing stream.
    for mod in _PAYLOAD_MODULES:
        import_module(mod)


_load_payload_modules()


@dataclass(frozen=True)
class StreamMapping:
    """How to interpret one StreamKind on the wire and in MCAP."""

    kind: int                   # StreamKind enum value (e.g. STREAM_IMU_RAW = 1)
    name: str                   # StreamKind enum name (e.g. "STREAM_IMU_RAW")
    proto_type: str             # protobuf full name (e.g. "visio_schema.sensor.v1.ImuRaw")
    mcap_schema_name: str       # MCAP Schema.name (usually == proto_type)


class SchemaRegistry:
    """Lookup table from StreamKind to StreamMapping, built from the
    generated `header_pb2.StreamKind` descriptor. Read-only after
    construction (threadsafe)."""

    def __init__(self) -> None:
        self._by_kind: dict[int, StreamMapping] = {}
        for v in _hdr.StreamKind.DESCRIPTOR.values:
            opts = v.GetOptions()
            proto_type = opts.Extensions[_hdr.visio_proto_type]
            if not proto_type:
                # STREAM_UNKNOWN, STREAM_TIMESYNC, STREAM_DEVICE_INFO,
                # STREAM_CUSTOM — left unannotated by design.
                continue
            mcap_name = opts.Extensions[_hdr.visio_mcap_schema_name] or proto_type
            self._by_kind[v.number] = StreamMapping(
                kind=v.number,
                name=v.name,
                proto_type=proto_type,
                mcap_schema_name=mcap_name,
            )

    def for_kind(self, kind: int) -> StreamMapping | None:
        """Return the mapping for a StreamKind, or None if unannotated."""
        return self._by_kind.get(kind)

    def __iter__(self) -> Iterator[StreamMapping]:
        return iter(self._by_kind.values())

    def __len__(self) -> int:
        return len(self._by_kind)


# Default shared registry. Cheap to build; one instance is plenty.
REGISTRY = SchemaRegistry()


def message_class(proto_type: str) -> type[_ProtoMessage]:
    """Return the generated protobuf message class for a full type name."""
    return symbol_database.Default().GetSymbol(proto_type)


@lru_cache(maxsize=None)
def file_descriptor_set(proto_type: str) -> bytes:
    """Serialized FileDescriptorSet for `proto_type` and its transitive
    dependencies — the bytes an MCAP Schema record or a Foxglove protobuf
    channel needs to self-describe the payload.

    Cached because the descriptor pool is immutable for the process
    lifetime and walking + serializing transitive deps is the dominant
    cost on the DeviceInfoService initiator path that ships descriptors
    for every declared OutputStream.
    """
    descriptor = symbol_database.Default().pool.FindMessageTypeByName(proto_type)
    seen: dict[str, FileDescriptor] = {}
    order: list[FileDescriptor] = []

    def visit(fd: FileDescriptor) -> None:
        if fd.name in seen:
            return
        seen[fd.name] = fd
        for dep in fd.dependencies:
            visit(dep)
        order.append(fd)

    visit(descriptor.file)
    fds = descriptor_pb2.FileDescriptorSet()
    for fd in order:
        fd.CopyToProto(fds.file.add())
    return fds.SerializeToString()


def _strip(name: str, prefix: str) -> str:
    return name[len(prefix):] if name.startswith(prefix) else name


def synthesized_topic(device: int, stream: int, stream_index: int) -> str:
    """`/{device}/{stream}/{stream_index}` presentation topic per
    MASTER_PLAN §5 — DEVICE_/STREAM_ prefixes stripped, lower-cased."""
    dname = _strip(_hdr.DeviceClass.Name(device), "DEVICE_").lower()
    sname = _strip(_hdr.StreamKind.Name(stream), "STREAM_").lower()
    return f"/{dname}/{sname}/{stream_index}"


def parse_topic(topic: str) -> tuple[int, int, int]:
    """Inverse of `synthesized_topic` → (device, stream, stream_index)."""
    parts = topic.strip("/").split("/")
    if len(parts) != 3:
        raise ValueError(f"not a synthesized visio topic: {topic!r}")
    dname, sname, idx = parts
    device = _hdr.DeviceClass.Value("DEVICE_" + dname.upper())
    stream = _hdr.StreamKind.Value("STREAM_" + sname.upper())
    return device, stream, int(idx)
