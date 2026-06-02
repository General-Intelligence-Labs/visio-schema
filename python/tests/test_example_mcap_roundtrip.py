"""Round-trip the example MCAP writer: generate a sample, read it back, and
verify topics, schemas, and message payloads survive.

This guards the example sink (visio_display.McapSink) + the sample generator
(make_sample_mcap.py) that users actually run, and the Foxglove schema-name
invariant (protobuf full name, resolvable inside the embedded FileDescriptorSet)
end-to-end through a real MCAP reader — now on the dynamic stream_id / Channel
model.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve().parent
_EXAMPLES = _THIS.parents[1] / "examples" / "python"
_SAMPLE_GEN = _EXAMPLES / "make_sample_mcap.py"

# These examples depend on the `mcap` library; skip cleanly if it's absent.
pytest.importorskip("mcap", reason="mcap library not installed")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sample_mcap_roundtrips(tmp_path) -> None:
    gen = _load("make_sample_mcap", _SAMPLE_GEN)
    out = tmp_path / "sample.mcap"
    counts = gen.generate(str(out), seconds=1.0)
    assert counts["imu"] > 0
    assert out.exists()

    from mcap.reader import make_reader

    topics: set[str] = set()
    schema_names: set[str] = set()
    n_messages = 0
    with open(out, "rb") as f:
        reader = make_reader(f)
        for schema, channel, _message in reader.iter_messages():
            n_messages += 1
            topics.add(channel.topic)
            if schema is not None:
                schema_names.add(schema.name)

    assert n_messages == sum(counts.values())
    # Topics follow the /<device>/<group>/<index>/<sub-field> convention.
    assert gen.IMU_RAW_TOPIC in topics
    assert gen.IMU_QUAT_TOPIC in topics
    # Foxglove invariant: the schema name is the protobuf full name (resolvable
    # inside the embedded FileDescriptorSet), NOT a ROS-style name.
    assert "visio_schema.sensor.v1.ImuRaw" in schema_names
    assert "visio_schema.ros.geometry_msgs.v1.Quaternion" in schema_names


def test_mcap_sink_drops_then_records_by_channel(tmp_path) -> None:
    """The sink keys MCAP channels on stream_id and pulls topic/schema from the
    Channel handed in — the resolve step the live reader performs."""
    fox = _load("visio_display", _EXAMPLES / "visio_display.py")
    from visio_schema.service.device_info.v1.device_info_pb2 import Channel
    from visio_schema.wire.message import Message
    from visio_schema.wire.streams import file_descriptor_set
    from visio_schema.wire.v1.header_pb2 import ControlStream

    sid = ControlStream.CONTROL_STREAM_FIRST_DYNAMIC
    ch = Channel(
        id=sid,
        topic="/glove_left/imus/0/raw",
        encoding="protobuf",
        schema_name="visio_schema.sensor.v1.ImuRaw",
        schema=file_descriptor_set("visio_schema.sensor.v1.ImuRaw"),
        schema_encoding="protobuf",
    )
    out = tmp_path / "sink.mcap"
    sink = fox.McapSink(str(out))
    msg = Message(stream_id=sid, seq=1, payload=b"raw-bytes")
    msg.timestamp.FromNanoseconds(1_700_000_000_000_000_000)
    sink.write(msg, ch)
    sink.close()

    from mcap.reader import make_reader

    with open(out, "rb") as f:
        rows = list(make_reader(f).iter_messages())
    assert len(rows) == 1
    schema, channel, message = rows[0]
    assert channel.topic == "/glove_left/imus/0/raw"
    assert schema.name == "visio_schema.sensor.v1.ImuRaw"
    assert message.data == b"raw-bytes"
