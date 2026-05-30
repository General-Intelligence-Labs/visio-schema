"""StreamKind reflection per visio-schema/docs/stream_type_map.md."""
from __future__ import annotations

from google.protobuf import descriptor_pb2

from visio.wire import streams
from visio.wire.v1.header_pb2 import DeviceClass, StreamKind


def test_imu_raw_mapping() -> None:
    m = streams.REGISTRY.for_kind(StreamKind.STREAM_IMU_RAW)
    assert m is not None
    assert m.proto_type == "visio.sensor.v1.ImuRaw"
    assert m.mcap_schema_name == "visio.sensor.v1.ImuRaw"


def test_imu_quat_ros_name_remap() -> None:
    m = streams.REGISTRY.for_kind(StreamKind.STREAM_IMU_QUAT)
    assert m is not None
    assert m.proto_type == "visio.ros.geometry_msgs.v1.Quaternion"
    assert m.mcap_schema_name == "geometry_msgs/msg/Quaternion"


def test_service_streams_unannotated() -> None:
    assert streams.REGISTRY.for_kind(StreamKind.STREAM_TIMESYNC) is None
    assert streams.REGISTRY.for_kind(StreamKind.STREAM_SCHEMA_QUERY) is None


def test_message_class_resolves() -> None:
    cls = streams.message_class("visio.sensor.v1.ImuRaw")
    assert cls.DESCRIPTOR.full_name == "visio.sensor.v1.ImuRaw"


def test_file_descriptor_set_is_parseable() -> None:
    raw = streams.file_descriptor_set("visio.sensor.v1.ImuRaw")
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(raw)
    names = {f.name for f in fds.file}
    # The type's own file plus its transitive deps (timestamp, etc.).
    assert any("imu_raw" in n for n in names)
    assert len(fds.file) >= 1


def test_topic_roundtrip() -> None:
    topic = streams.synthesized_topic(
        DeviceClass.DEVICE_GLOVE_LEFT, StreamKind.STREAM_IMU_RAW, 3
    )
    assert topic == "/glove_left/imu_raw/3"
    assert streams.parse_topic(topic) == (
        DeviceClass.DEVICE_GLOVE_LEFT,
        StreamKind.STREAM_IMU_RAW,
        3,
    )
