"""McapWriter + read_mcap — the canonical visio-schema recording path.

Writing ``(channel, message)`` pairs produces a spec-conformant, Foxglove-readable
MCAP (schema name = protobuf full name, schema data = the embedded
FileDescriptorSet), and reading it back yields the same ``(Message, Channel)``
shape a live ``ChannelRegistry.resolved`` stream produces. Rotation splits into
self-contained numbered parts.
"""
from __future__ import annotations

import io

import pytest

pytest.importorskip("mcap", reason="mcap library not installed")

from visio_schema.mcap import McapWriter, read_mcap
from visio_schema.routing import FIRST_DYNAMIC
from visio_schema.v1.service.device_info.device_info_pb2 import Channel
from visio_schema.wire.message import Message
from visio_schema.wire.schema import file_descriptor_set

_IMU = "visio_schema.v1.sensor.ImuRaw"


def _channel(cid: int = FIRST_DYNAMIC, topic: str = "/dev/imus/0/raw") -> Channel:
    return Channel(
        id=cid, topic=topic, encoding="protobuf",
        schema_name=_IMU, schema=file_descriptor_set(_IMU),
        schema_encoding="protobuf",
    )


def _msg(cid: int, i: int, payload: bytes) -> Message:
    m = Message(stream_id=cid, payload=payload, seq=i)
    m.timestamp.FromNanoseconds(1_700_000_000_000_000_000 + i)
    return m


def test_round_trip_records_and_reads(tmp_path) -> None:
    ch = _channel()
    out = tmp_path / "rec.mcap"
    with McapWriter(out) as w:
        for i in range(5):
            w.write(ch, _msg(ch.id, i, f"imu-{i}".encode()))

    rows = list(read_mcap(out))
    assert len(rows) == 5
    for i, (m, c) in enumerate(rows):
        assert c.topic == "/dev/imus/0/raw"
        assert c.schema_name == _IMU and len(c.schema) > 0   # Foxglove invariant
        assert m.payload == f"imu-{i}".encode() and m.seq == i


def test_bytesio_sink_records() -> None:
    ch = _channel()
    buf = io.BytesIO()
    w = McapWriter(buf)
    w.write(ch, _msg(ch.id, 0, b"x"))
    w.close()
    assert buf.getvalue()[:8] == b"\x89MCAP0\r\n"   # MCAP magic; not closed by us
    assert not buf.closed


def test_non_seekable_sink_rejected() -> None:
    class _Pipe(io.RawIOBase):
        def seekable(self): return False
    with pytest.raises(ValueError):
        McapWriter(_Pipe())


def test_rotation_into_self_contained_parts(tmp_path) -> None:
    ch = _channel()
    base = tmp_path / "run.mcap"
    # ~16 B payloads, roll every 40 B -> ~3 messages/part across 10 messages.
    with McapWriter(base, max_bytes=40) as w:
        for i in range(10):
            w.write(ch, _msg(ch.id, i, b"x" * 16))

    parts = sorted(tmp_path.glob("run_*.mcap"))
    assert len(parts) >= 3
    assert not base.exists()   # rotating uses numbered parts, not the bare name

    # Each part stands alone: re-registers its schema, reads back independently.
    total = 0
    for part in parts:
        rows = list(read_mcap(part))
        assert rows, f"{part} empty"
        assert all(c.schema_name == _IMU and c.schema for _, c in rows)
        total += len(rows)
    assert total == 10
