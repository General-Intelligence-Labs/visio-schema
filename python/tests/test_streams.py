"""StreamKind reflection per visio-schema/docs/stream_type_map.md."""
from __future__ import annotations

from google.protobuf import descriptor_pb2

from visio_schema.wire import streams
from visio_schema.wire.v1.header_pb2 import DeviceClass, StreamKind


def test_imu_raw_mapping() -> None:
    m = streams.REGISTRY.for_kind(StreamKind.STREAM_IMU_RAW)
    assert m is not None
    assert m.proto_type == "visio_schema.sensor.v1.ImuRaw"
    assert m.mcap_schema_name == "visio_schema.sensor.v1.ImuRaw"


def test_imu_quat_ros_name_remap() -> None:
    m = streams.REGISTRY.for_kind(StreamKind.STREAM_IMU_QUAT)
    assert m is not None
    assert m.proto_type == "visio_schema.ros.geometry_msgs.v1.Quaternion"
    assert m.mcap_schema_name == "geometry_msgs/msg/Quaternion"


def test_service_streams_unannotated() -> None:
    # STREAM_TIMESYNC and STREAM_DEVICE_INFO are dual-payload service
    # streams (Request / Response). Service code hand-codes the two-type
    # dispatch, so the registry returns None for them.
    assert streams.REGISTRY.for_kind(StreamKind.STREAM_TIMESYNC) is None
    assert streams.REGISTRY.for_kind(StreamKind.STREAM_DEVICE_INFO) is None


def test_imu_calibration_mapping() -> None:
    m = streams.REGISTRY.for_kind(StreamKind.STREAM_IMU_CALIBRATION)
    assert m is not None
    assert m.proto_type == "visio_schema.calibration.v1.ImuCalibration"


def test_camera_calibration_mapping() -> None:
    m = streams.REGISTRY.for_kind(StreamKind.STREAM_CAMERA_CALIB)
    assert m is not None
    assert m.proto_type == "foxglove.CameraCalibration"


def test_message_class_resolves() -> None:
    cls = streams.message_class("visio_schema.sensor.v1.ImuRaw")
    assert cls.DESCRIPTOR.full_name == "visio_schema.sensor.v1.ImuRaw"


def test_file_descriptor_set_is_parseable() -> None:
    raw = streams.file_descriptor_set("visio_schema.sensor.v1.ImuRaw")
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(raw)
    names = {f.name for f in fds.file}
    # The type's own file plus its transitive deps (timestamp, etc.).
    assert any("imu_raw" in n for n in names)
    assert len(fds.file) >= 1


def test_file_descriptor_set_for_imu_calibration() -> None:
    """The new STREAM_IMU_CALIBRATION payload type round-trips through
    `file_descriptor_set`. Catches a botched proto codegen for the new
    visio_schema/calibration/v1/imu.proto file."""
    raw = streams.file_descriptor_set("visio_schema.calibration.v1.ImuCalibration")
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(raw)
    names = {f.name for f in fds.file}
    # The type's own file + its Foxglove Pose dependency.
    assert any("calibration/v1/imu.proto" in n for n in names)
    assert any("foxglove/Pose.proto" in n for n in names)


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
