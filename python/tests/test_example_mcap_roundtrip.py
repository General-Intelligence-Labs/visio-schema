"""Round-trip the example MCAP writer: generate a sample, read it back, and
verify topics, schemas, and message payloads survive.

This guards the canonical writer (visio_schema.mcap.McapWriter) the example
now uses + the sample generator (make_sample_mcap.py) that users actually run, and
the Foxglove schema-name
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
    assert "visio_schema.v1.sensor.ImuRaw" in schema_names
    assert "visio_schema.v1.ros.geometry_msgs.Quaternion" in schema_names


def test_writer_records_by_channel(tmp_path) -> None:
    """McapWriter pulls topic/schema from the Channel handed in (the resolve step
    the live reader performs) and writes the payload verbatim — what the example
    relies on for the --out sink."""
    from visio_schema.mcap import McapWriter, read_mcap
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel
    from visio_schema.v1.wire.header_pb2 import ControlStream
    from visio_schema.wire.message import Message
    from visio_schema.wire.schema import file_descriptor_set

    sid = ControlStream.CONTROL_STREAM_FIRST_DYNAMIC
    ch = Channel(
        id=sid,
        topic="/glove_left/imus/0/raw",
        encoding="protobuf",
        schema_name="visio_schema.v1.sensor.ImuRaw",
        schema=file_descriptor_set("visio_schema.v1.sensor.ImuRaw"),
        schema_encoding="protobuf",
    )
    out = tmp_path / "sink.mcap"
    msg = Message(stream_id=sid, seq=1, payload=b"raw-bytes")
    msg.timestamp.FromNanoseconds(1_700_000_000_000_000_000)
    with McapWriter(str(out)) as w:
        w.write(msg, ch)

    rows = list(read_mcap(out))
    assert len(rows) == 1
    rmsg, rch = rows[0]
    assert rch.topic == "/glove_left/imus/0/raw"
    assert rch.schema_name == "visio_schema.v1.sensor.ImuRaw"
    assert rmsg.payload == b"raw-bytes" and rmsg.seq == 1


def _one(tmp_path, name, *, chan_id, topic, schema_name, payload):
    """Write a single-message MCAP, letting the caller pick the channel id."""
    from visio_schema.mcap import McapWriter
    from visio_schema.v1.service.device_info.device_info_pb2 import Channel
    from visio_schema.wire.message import Message
    from visio_schema.wire.schema import file_descriptor_set

    ch = Channel(
        id=chan_id, topic=topic, encoding="protobuf", schema_name=schema_name,
        schema=file_descriptor_set(schema_name), schema_encoding="protobuf",
    )
    msg = Message(stream_id=chan_id, seq=1, payload=payload)
    msg.timestamp.FromNanoseconds(1_700_000_000_000_000_000)
    out = tmp_path / name
    with McapWriter(str(out)) as w:
        w.write(msg, ch)
    return out


def test_merge_of_sources_with_colliding_channel_ids(tmp_path) -> None:
    """Channels dedupe by TOPIC, not by `Channel.id`.

    Regression: `Channel.id` is only unique within one file — `read_mcap` fills it
    with the MCAP channel id — so two independently written sources both start at 1.
    Keying the writer's channel cache on it made the second source's messages inherit
    the FIRST source's topic and schema: merging a recording with a VIO sidecar wrote
    3810 `foxglove.PoseInFrame` payloads onto `/ego/imu/0/quat` labelled as
    `Quaternion`, with no error raised and nothing to notice at read time.
    """
    from visio_schema.mcap import McapWriter, read_mcap

    a = _one(tmp_path, "a.mcap", chan_id=1, topic="/ego/imu/0/quat",
             schema_name="visio_schema.v1.ros.geometry_msgs.Quaternion",
             payload=b"quat-bytes")
    b = _one(tmp_path, "b.mcap", chan_id=1, topic="/ego/vio/pose",
             schema_name="foxglove.PoseInFrame", payload=b"pose-bytes")
    # the ids really do collide — the precondition the bug needed
    assert {ch.id for _m, ch in read_mcap(a)} == {ch.id for _m, ch in read_mcap(b)}

    merged = tmp_path / "merged.mcap"
    with McapWriter(str(merged)) as w:
        for src in (a, b):
            for msg, ch in read_mcap(src):
                w.write(msg, ch)

    rows = {ch.topic: (ch.schema_name, msg.payload) for msg, ch in read_mcap(merged)}
    assert set(rows) == {"/ego/imu/0/quat", "/ego/vio/pose"}, rows
    assert rows["/ego/vio/pose"] == ("foxglove.PoseInFrame", b"pose-bytes")
    assert rows["/ego/imu/0/quat"][0] == "visio_schema.v1.ros.geometry_msgs.Quaternion"


def test_same_topic_with_two_schemas_is_refused(tmp_path) -> None:
    """A topic carrying two schemas is an unreadable file — fail, don't silently bind
    to whichever arrived first."""
    import pytest

    from visio_schema.mcap import McapWriter, read_mcap

    a = _one(tmp_path, "a.mcap", chan_id=1, topic="/ego/thing",
             schema_name="visio_schema.v1.ros.geometry_msgs.Quaternion", payload=b"q")
    b = _one(tmp_path, "b.mcap", chan_id=7, topic="/ego/thing",
             schema_name="foxglove.PoseInFrame", payload=b"p")
    with pytest.raises(ValueError, match="already registered with schema"):
        with McapWriter(str(tmp_path / "bad.mcap")) as w:
            for src in (a, b):
                for msg, ch in read_mcap(src):
                    w.write(msg, ch)
