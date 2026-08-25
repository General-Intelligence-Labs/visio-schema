"""Behavior of the facade convenience helpers added to the public API:
``command_message``, ``make_channel``, and the live ``read_serial`` source."""
from __future__ import annotations

import os

import pytest
from google.protobuf.descriptor_pb2 import FileDescriptorSet

from visio_schema import COMMAND, command_message, make_channel, message_class, read_serial
from visio_schema.transport import make_fd_pair
from visio_schema.transport.framing import frame_bytes
from visio_schema.v1.control import command_pb2
from visio_schema.v1.service.device_info.device_info_pb2 import DeviceInfo
from visio_schema.wire.control import DEVICE_INFO
from visio_schema.wire.message import Message


def test_command_message_wraps_onto_command_stream():
    """command_message puts a Command on the COMMAND stream and preserves its bytes."""
    cmd = command_pb2.Command(
        target_device="ego",
        command_id=7,
        start_recording=command_pb2.StartRecording(session_name="demo"),
    )
    msg = command_message(cmd)

    assert msg.stream_id == COMMAND
    decoded = command_pb2.Command.FromString(msg.payload)
    assert decoded.target_device == "ego"
    assert decoded.command_id == 7
    assert decoded.WhichOneof("body") == "start_recording"
    assert decoded.start_recording.session_name == "demo"


def test_make_channel_is_self_describing_and_writable():
    """make_channel fills a resolvable schema name + its FileDescriptorSet bytes."""
    ch = make_channel("/imu/0/raw", "visio_schema.v1.sensor.ImuRaw", stream_id=16)

    assert ch.id == 16
    assert ch.topic == "/imu/0/raw"
    assert ch.schema_name == "visio_schema.v1.sensor.ImuRaw"
    assert ch.encoding == "protobuf" and ch.schema_encoding == "protobuf"
    assert ch.schema, "schema (FileDescriptorSet) must be populated"
    # the embedded schema must be a parseable FileDescriptorSet that defines the type
    fds = FileDescriptorSet.FromString(ch.schema)
    assert any(m.name == "ImuRaw" for f in fds.file for m in f.message_type)
    # schema_name must resolve to a real generated type (what a reader decodes with)
    assert message_class(ch.schema_name) is not None


@pytest.mark.pty  # waits on pty readability via read_serial's select loop — see tests/conftest.py
def test_read_serial_resolves_announced_topics():
    """read_serial yields resolved (message, channel) rows: a DeviceInfo announce is
    learned and absorbed, then a data message on that stream comes through with its
    Channel. Driven over a pty (no hardware)."""
    master, slave = make_fd_pair()
    ch = make_channel("/imu/0/raw", "visio_schema.v1.sensor.ImuRaw", stream_id=16)
    announce = Message(
        stream_id=DEVICE_INFO,
        payload=DeviceInfo(device_name="t", channels=[ch]).SerializeToString(),
    )
    data = Message(stream_id=16, payload=b"raw-bytes")

    gen = read_serial(os.ttyname(slave))
    try:
        os.write(master, frame_bytes(announce) + frame_bytes(data))
        msg, channel = next(gen)            # blocks on the selector until the frames arrive
        assert channel.topic == "/imu/0/raw"
        assert msg.stream_id == 16
        assert msg.payload == b"raw-bytes"
    finally:
        gen.close()                          # GeneratorExit -> closes the opened fd
        os.close(master)
        os.close(slave)


def test_stamped_carries_nanoseconds_verbatim():
    """``Message.stamped`` is the write-side mirror of ``message_class``: it puts a
    payload on the recording's clock. The round trip through the protobuf
    Timestamp must be exact — a lost nanosecond here shifts a derived message off
    the source frame it was computed from."""
    t_ns = 1_700_000_000_123_456_789
    msg = Message.stamped(b"payload", t_ns)
    assert msg.timestamp.ToNanoseconds() == t_ns
    assert msg.payload == b"payload"
    assert (msg.seq, msg.stream_id) == (0, 0)   # caller-supplied, never inferred


def test_stamped_serializes_a_protobuf_message():
    """Accepts a message as well as bytes, so a builder's output goes straight in."""
    cmd = command_pb2.Command()
    cmd.identify.SetInParent()
    msg = Message.stamped(cmd, 7, seq=3, stream_id=42)
    assert msg.payload == cmd.SerializeToString()
    assert (msg.seq, msg.stream_id) == (3, 42)


def test_stamped_round_trips_through_the_wire_codec():
    """A stamped Message is a normal Message — it survives encode/decode intact."""
    from visio_schema.wire.message import decode_message, encode_message

    original = Message.stamped(b"abc", 1_234_567_890, seq=9, stream_id=17)
    back = decode_message(encode_message(original))
    assert back.payload == b"abc"
    assert back.seq == 9
    assert back.timestamp.ToNanoseconds() == 1_234_567_890
