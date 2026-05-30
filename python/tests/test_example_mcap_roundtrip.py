"""Round-trip the Python example's MCAP writer through its reader.

Loads examples/python/visio_foxglove.py by path (it is a script, not part of
the package) and exercises McapSink -> read_mcap. Skipped if `mcap` isn't
installed.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("mcap")

from mcap.reader import make_reader  # noqa: E402

from visio.wire.message import Message  # noqa: E402
from visio.wire.streams import message_class  # noqa: E402
from visio.wire.v1.header_pb2 import DeviceClass, StreamKind  # noqa: E402

_EXAMPLE = (
    Path(__file__).resolve().parents[2] / "examples" / "python" / "visio_foxglove.py"
)
_spec = importlib.util.spec_from_file_location("visio_foxglove", _EXAMPLE)
example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(example)


def _imu_raw_payload() -> bytes:
    imu = message_class("visio.sensor.v1.ImuRaw")()
    s = imu.samples.add()
    s.t_offset_ns = 1000
    s.angular_velocity.x = 0.5
    s.linear_acceleration.z = 9.81
    return imu.SerializeToString()


def _quat_payload() -> bytes:
    q = message_class("visio.ros.geometry_msgs.v1.Quaternion")()
    q.w = 1.0
    return q.SerializeToString()


def test_mcap_write_read_roundtrip(tmp_path) -> None:
    path = str(tmp_path / "run.mcap")
    msgs = [
        Message(
            stream=StreamKind.STREAM_IMU_RAW,
            stream_index=3,
            payload=_imu_raw_payload(),
            device=DeviceClass.DEVICE_GLOVE_LEFT,
            seq=1,
        ),
        Message(
            stream=StreamKind.STREAM_IMU_QUAT,
            stream_index=3,
            payload=_quat_payload(),
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

    back = list(example.read_mcap(path))
    assert len(back) == 2
    for original, decoded in zip(msgs, back):
        assert decoded.stream == original.stream
        assert decoded.stream_index == original.stream_index
        assert decoded.device == original.device
        assert decoded.seq == original.seq
        assert decoded.payload == original.payload
        assert decoded.timestamp.seconds == original.timestamp.seconds


def test_imu_quat_registered_under_ros_name(tmp_path) -> None:
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

    with open(path, "rb") as f:
        schema_names = {
            schema.name
            for schema, _channel, _message in make_reader(f).iter_messages()
        }
    assert "geometry_msgs/msg/Quaternion" in schema_names
