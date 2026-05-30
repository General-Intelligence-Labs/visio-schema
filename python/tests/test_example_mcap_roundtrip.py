"""Exercise the Python example's McapSink.

Loads examples/python/visio_foxglove.py by path (it is a script, not part of
the package) and writes messages through McapSink, then reads the MCAP back
directly. Skipped if `mcap` isn't installed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("mcap")

from mcap.reader import make_reader  # noqa: E402

from visio_schema.wire.message import Message  # noqa: E402
from visio_schema.wire.streams import message_class  # noqa: E402
from visio_schema.wire.v1.header_pb2 import DeviceClass, StreamKind  # noqa: E402

_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "examples" / "python" / "visio_foxglove.py"
)
_spec = importlib.util.spec_from_file_location("visio_foxglove", _EXAMPLE)
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)


def _imu_raw_payload() -> bytes:
    imu = message_class("visio_schema.sensor.v1.ImuRaw")()
    s = imu.samples.add()
    s.t_offset_ns = 1000
    s.angular_velocity.x = 0.5
    s.linear_acceleration.z = 9.81
    return imu.SerializeToString()


def _quat_payload() -> bytes:
    q = message_class("visio_schema.ros.geometry_msgs.v1.Quaternion")()
    q.w = 1.0
    return q.SerializeToString()


def test_mcap_write_roundtrip(tmp_path) -> None:
    """McapSink writes the synthesized topics, sequences, log-times, and
    payload bytes verbatim — read back straight from the MCAP."""
    path = str(tmp_path / "run.mcap")
    raw = _imu_raw_payload()
    quat = _quat_payload()
    msgs = [
        Message(
            stream=StreamKind.STREAM_IMU_RAW,
            stream_index=3,
            payload=raw,
            device=DeviceClass.DEVICE_GLOVE_LEFT,
            seq=1,
        ),
        Message(
            stream=StreamKind.STREAM_IMU_QUAT,
            stream_index=3,
            payload=quat,
            device=DeviceClass.DEVICE_GLOVE_LEFT,
            seq=2,
        ),
    ]
    for m in msgs:
        m.timestamp.seconds = 1_700_000_000 + m.seq

    sink = example.McapSink(path)
    for m in msgs:
        sink.write(m)
    sink.close()

    by_topic = {}
    with open(path, "rb") as f:
        for _schema, channel, message in make_reader(f).iter_messages():
            by_topic[channel.topic] = (message.sequence, message.log_time, bytes(message.data))

    assert by_topic["/glove_left/imu_raw/3"] == (1, 1_700_000_001_000_000_000, raw)
    assert by_topic["/glove_left/imu_quat/3"] == (2, 1_700_000_002_000_000_000, quat)


def test_protobuf_schema_name_is_resolvable(tmp_path) -> None:
    """A protobuf channel's schema name must equal the protobuf full name so
    Foxglove can resolve the type from the FileDescriptorSet. (The ROS-name
    remap in mcap_schema_name applies only to ros2msg-encoded channels.) We
    mirror Foxglove's resolution: parse each schema's descriptor set and look
    its own name up as a message type."""
    from google.protobuf import descriptor_pb2, descriptor_pool

    path = str(tmp_path / "quat.mcap")
    sink = example.McapSink(path)
    sink.write(
        Message(
            stream=StreamKind.STREAM_IMU_QUAT,
            stream_index=0,
            payload=_quat_payload(),
            device=DeviceClass.DEVICE_GLOVE_LEFT,
            seq=1,
        )
    )
    sink.close()

    seen = {}
    with open(path, "rb") as f:
        for schema, _channel, _message in make_reader(f).iter_messages():
            seen[schema.name] = schema.data

    assert "visio_schema.ros.geometry_msgs.v1.Quaternion" in seen
    for name, data in seen.items():
        pool = descriptor_pool.DescriptorPool()
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(data)
        for fdp in fds.file:
            pool.Add(fdp)
        # Must not raise KeyError — this is the check Foxglove performs.
        pool.FindMessageTypeByName(name)
