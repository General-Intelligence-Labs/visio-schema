"""McapWriter + read_mcap — the canonical visio-schema recording path.

Writing ``(message, channel)`` pairs produces a spec-conformant, Foxglove-readable
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
from visio_schema.routing import FIRST_DYNAMIC, Channel, make_channel
from visio_schema.wire.message import Message

_IMU = "visio_schema.v1.sensor.ImuRaw"


def _channel(cid: int = FIRST_DYNAMIC, topic: str = "/dev/imus/0/raw") -> Channel:
    return make_channel(topic, _IMU, stream_id=cid)


def _msg(cid: int, i: int, payload: bytes) -> Message:
    return Message.stamped(
        payload, 1_700_000_000_000_000_000 + i, seq=i, stream_id=cid
    )


def test_round_trip_records_and_reads(tmp_path) -> None:
    ch = _channel()
    out = tmp_path / "rec.mcap"
    with McapWriter(out) as w:
        for i in range(5):
            w.write(_msg(ch.id, i, f"imu-{i}".encode()), ch)

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
    w.write(_msg(ch.id, 0, b"x"), ch)
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
            w.write(_msg(ch.id, i, b"x" * 16), ch)

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


def test_part_names_are_4_digit_and_sort_past_999(tmp_path) -> None:
    """Part names zero-pad to 4 digits (mirroring the C++ writer's NumberedPart) so
    part 1000 sorts *after* 999. The 3-digit pad this replaced broke the lexical order
    the uploader and playback rely on once a session exceeds 999 parts — glob+sort would
    put ``_1000`` before ``_999``. A white-box check on the name (no need to spill 1000
    real parts): the rotation test above already covers the 0..N happy path."""
    with McapWriter(tmp_path / "run.mcap", max_bytes=40) as w:
        w._part_index = 999
        p999 = w._part_path().name
        w._part_index = 1000
        p1000 = w._part_path().name
    assert (p999, p1000) == ("run_0999.mcap", "run_1000.mcap")
    assert sorted([p1000, p999]) == [p999, p1000]   # 999 lexically precedes 1000


def _metadata_of(path) -> dict[str, dict[str, str]]:
    """Every metadata record in an MCAP, by name."""
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        return {m.name: dict(m.metadata) for m in make_reader(f).iter_metadata()}


def test_metadata_record_is_written_and_readable(tmp_path) -> None:
    """Provenance rides as an MCAP metadata record, so a consumer can read it
    without decoding a single message."""
    ch = _channel()
    out = tmp_path / "rec.mcap"
    with McapWriter(out) as w:
        w.add_metadata("visio.derived", {"stage": "depth", "version": "0.2.0"})
        w.write(_msg(ch.id, 0, b"imu"), ch)

    assert _metadata_of(out)["visio.derived"] == {
        "stage": "depth",
        "version": "0.2.0",
    }


def test_metadata_replayed_into_every_rotated_part(tmp_path) -> None:
    """A rolled part must stand alone — including its provenance. Without the
    replay, part 1 carries the record and every later part silently loses it, so
    a consumer reading part 3 cannot tell what produced it."""
    ch = _channel()
    base = tmp_path / "run.mcap"
    with McapWriter(base, max_bytes=40) as w:
        w.add_metadata("visio.derived", {"stage": "depth"})
        for i in range(10):
            w.write(_msg(ch.id, i, b"x" * 16), ch)

    parts = sorted(tmp_path.glob("run_*.mcap"))
    assert len(parts) >= 3
    for part in parts:
        assert _metadata_of(part)["visio.derived"] == {"stage": "depth"}


def test_metadata_added_mid_run_reaches_later_parts(tmp_path) -> None:
    """Records registered after some parts already closed still propagate
    forward; the earlier parts legitimately predate them."""
    ch = _channel()
    base = tmp_path / "run.mcap"
    with McapWriter(base, max_bytes=40) as w:
        for i in range(5):
            w.write(_msg(ch.id, i, b"x" * 16), ch)
        w.add_metadata("late", {"k": "v"})
        for i in range(5, 10):
            w.write(_msg(ch.id, i, b"x" * 16), ch)

    parts = sorted(tmp_path.glob("run_*.mcap"))
    assert _metadata_of(parts[-1])["late"] == {"k": "v"}


def test_metadata_rejects_non_string_value(tmp_path) -> None:
    """MCAP metadata is strictly string->string. Reject at the call site, naming
    the key, instead of failing obscurely inside the mcap writer."""
    with McapWriter(tmp_path / "rec.mcap") as w:
        with pytest.raises(TypeError, match="frames"):
            w.add_metadata("visio.derived", {"frames": 42})


def _header_of(path):
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        return make_reader(f).get_header()


def test_header_defaults_leave_existing_output_untouched(tmp_path) -> None:
    """`profile`/`library` were added for the derived-writer path. Their defaults
    must reproduce what this writer produced before they existed — otherwise
    adding them restamps the header of every recording ever written by it."""
    ch = _channel()
    out = tmp_path / "rec.mcap"
    with McapWriter(out) as w:
        w.write(_msg(ch.id, 0, b"imu"), ch)
    assert _header_of(out).profile == ""


def test_header_carries_profile_and_library_into_every_part(tmp_path) -> None:
    """A rolled part must stand alone, and 'which tool wrote this' is the first
    question asked when two files differ — so it belongs in every part, not just
    the first."""
    ch = _channel()
    base = tmp_path / "run.mcap"
    with McapWriter(base, max_bytes=40, profile="visio", library="thing@1") as w:
        for i in range(10):
            w.write(_msg(ch.id, i, b"x" * 16), ch)

    parts = sorted(tmp_path.glob("run_*.mcap"))
    assert len(parts) >= 3
    for part in parts:
        header = _header_of(part)
        assert (header.profile, header.library) == ("visio", "thing@1"), part.name
