"""Schema-pool helpers: resolve a proto type name to its message class and its
serialized FileDescriptorSet.

With dynamic, string-named streams the wire no longer carries a ``StreamKind``
enum. A stream's schema is identified by its fully-qualified protobuf type name
(carried in ``DeviceInfo.Channel.schema_name``) and shipped inline as a
serialized ``FileDescriptorSet`` (``Channel.schema``). These helpers build both
from the default descriptor pool, so a producer can populate a ``Channel`` and a
consumer (the MCAP recorder, a Foxglove bridge) can resolve a type with no
hand-maintained mapping table.

Importing this module eagerly loads every generated payload module so the
default descriptor pool can resolve any payload type by name.
"""
from __future__ import annotations

from functools import lru_cache
from importlib import import_module

from google.protobuf import descriptor_pb2, symbol_database
from google.protobuf.descriptor import FileDescriptor
from google.protobuf.message import Message as _ProtoMessage

__all__ = ["message_class", "file_descriptor_set"]

# Generated payload modules. Importing them registers their descriptors in the
# default pool so message_class() / file_descriptor_set() can resolve any
# payload type by name. Mirrors visio-schema/tests/test_imports.py; extend both
# when a new payload type is added.
_PAYLOAD_MODULES = (
    "visio_schema.v1.sensor.imu_raw_pb2",
    "visio_schema.v1.sensor.encoder_raw_pb2",
    "visio_schema.v1.sensor.system_health_pb2",
    "visio_schema.v1.sensor.audio_compressed_pb2",
    "visio_schema.v1.sensor.button_pb2",
    "visio_schema.v1.calibration.imu_pb2",
    "visio_schema.v1.calibration.encoder_pb2",
    "visio_schema.v1.ros.geometry_msgs.quaternion_pb2",
    "visio_schema.v1.input.quest_controller_state_pb2",
    "visio_schema.v1.geometry.twist_pb2",
    "visio_schema.v1.control.command_pb2",
    "visio_schema.v1.control.command_result_pb2",
    "visio_schema.v1.service.device_info.device_info_pb2",
    "visio_schema.v1.service.heartbeat.heartbeat_pb2",
    "visio_schema.v1.service.ota.ota_pb2",
    "visio_schema.foxglove.CompressedVideo_pb2",
    "visio_schema.foxglove.CompressedImage_pb2",
    "visio_schema.foxglove.RawImage_pb2",
    "visio_schema.foxglove.CameraCalibration_pb2",
    "visio_schema.foxglove.ImageAnnotations_pb2",
    "visio_schema.foxglove.RawAudio_pb2",
    "visio_schema.foxglove.PoseInFrame_pb2",
    "visio_schema.foxglove.FrameTransform_pb2",
    "visio_schema.foxglove.FrameTransforms_pb2",
    "visio_schema.foxglove.JointStates_pb2",
    "visio_schema.foxglove.Log_pb2",
    "visio_schema.foxglove.SceneUpdate_pb2",
)


def _load_payload_modules() -> None:
    # Import eagerly and let ImportError propagate: a missing module means
    # codegen is incomplete (run `make gen`). Swallowing it would leave the
    # pool silently half-populated and only fail much later, at the first MCAP
    # write or Foxglove publish of the missing stream.
    for mod in _PAYLOAD_MODULES:
        import_module(mod)


_load_payload_modules()


def message_class(proto_type: str) -> type[_ProtoMessage]:
    """Resolve a protobuf type name to its generated message class.

    Decode a payload whose type you learned at runtime (a `Channel.schema_name`)
    without importing the generated module yourself.

    Args:
        proto_type: The full protobuf type name, e.g.
            ``"visio_schema.v1.sensor.ImuRaw"``.

    Returns:
        The generated message class — instantiate it and ``ParseFromString`` the
        payload.

    Raises:
        KeyError: If `proto_type` is not a known generated type.

    Example:
        payload = message_class(channel.schema_name)()
        payload.ParseFromString(msg.payload)
    """
    return symbol_database.Default().GetSymbol(proto_type)


@lru_cache(maxsize=None)
def file_descriptor_set(proto_type: str) -> bytes:
    """Serialized ``FileDescriptorSet`` for a type and its transitive dependencies.

    These are the bytes a `Channel`'s `schema` carries so an MCAP Schema record or a
    Foxglove protobuf channel can self-describe the payload (files emitted
    dependencies-first). Most users get this via `make_channel`, which fills it in;
    reach for it directly only when building a `Channel` by hand.

    Args:
        proto_type: The full protobuf type name, e.g.
            ``"visio_schema.v1.sensor.ImuRaw"``.

    Returns:
        The serialized ``google.protobuf.FileDescriptorSet`` bytes.

    Example:
        fds = file_descriptor_set("visio_schema.v1.sensor.ImuRaw")

    Cached: the descriptor pool is immutable for the process, and walking +
    serializing transitive deps dominates the announce path.
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
