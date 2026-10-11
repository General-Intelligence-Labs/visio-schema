"""VIVE batching: cross-language bytes, stable identities, translated clocks."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from visio_schema import McapWriter, Message, make_channel, read_mcap
from visio_schema.v1.sensor.vive_pb2 import VivePoseBatch, ViveStatus

ROOT = Path(__file__).resolve().parents[2]
VECTORS = dict(
    line.split() for line in (ROOT / "tests/golden/vive_vectors.txt").read_text().splitlines()
    if line and not line.startswith("#")
)
spec = spec_from_file_location("read_vive_example", ROOT / "examples/python/read_vive.py")
example = module_from_spec(spec)
spec.loader.exec_module(example)


def test_pose_golden_and_clock_translation():
    data = bytes.fromhex(VECTORS["poses"])
    batch = VivePoseBatch.FromString(data)
    assert batch.SerializeToString() == data
    assert batch.first_sample_time.ToNanoseconds() == 1_000_000_000
    channel = make_channel("/ego/vive/poses", "visio_schema.v1.sensor.VivePoseBatch", stream_id=16)
    message = Message(stream_id=16, payload=data, seq=1)
    message.timestamp.FromNanoseconds(9_000_000_000)
    rows = list(example.iter_poses(message, channel))
    assert [(r[0], r[1]) for r in rows] == [
        ("LHR-A", 9_000_000_000), ("LHR-A", 9_000_000_020), ("LHR-B", 9_000_000_010),
    ]
    assert all(r[2:4] == ("vive_world", "boot-test") for r in rows)
    assert rows[2][4].position.z == 6
    assert rows[2][4].orientation.w == 0.5


def test_status_golden_has_no_slots_and_preserves_optional_presence():
    data = bytes.fromhex(VECTORS["status"])
    status = ViveStatus.FromString(data)
    assert status.SerializeToString() == data
    assert status.expected_tracker_count == status.dongle_count == 3
    assert status.live_tracker_count == 2
    a, b = status.trackers
    assert "slot" not in a.DESCRIPTOR.fields_by_name
    assert "timestamp" not in a.DESCRIPTOR.fields_by_name
    assert a.HasField("pose_age_ns") and a.pose_age_ns == 0
    assert a.HasField("charging") and not a.charging
    assert not b.HasField("pose_age_ns")
    assert not b.HasField("battery_pct")


def test_wired_status_counts_are_distinct():
    status = ViveStatus(expected_tracker_count=3, wired_tracker_count=3, live_tracker_count=3)
    decoded = ViveStatus.FromString(status.SerializeToString())
    assert decoded.dongle_count == 0
    assert decoded.wired_tracker_count == decoded.live_tracker_count == 3


def test_finalized_mcap_roundtrip(tmp_path):
    path = tmp_path / "vive.mcap"
    channel = make_channel("/ego/vive/poses", "visio_schema.v1.sensor.VivePoseBatch", stream_id=16)
    message = Message(stream_id=16, seq=1, payload=bytes.fromhex(VECTORS["poses"]))
    message.timestamp.FromNanoseconds(9_000_000_000)
    with McapWriter(path) as writer:
        writer.write(message, channel)
    rows = list(read_mcap(path))
    assert len(rows) == 1
    decoded = list(example.iter_poses(*rows[0]))
    assert len(decoded) == 3
    assert decoded[0][0:2] == ("LHR-A", 9_000_000_000)
