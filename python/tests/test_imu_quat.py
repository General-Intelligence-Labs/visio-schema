"""A bundled orientation preserves sample values and aligned acquisition times."""
from __future__ import annotations

import numpy as np
import pytest
from google.protobuf import descriptor_pb2

from visio_schema import McapWriter, Message, make_channel, message_class, read_mcap
from visio_schema.reader import Orientation, Session, elements
from visio_schema.reader.adapters import AdapterContext, build_adapter
from visio_schema.v1.sensor.imu_quat_pb2 import ImuQuat
from visio_schema.wire.schema import file_descriptor_set

SCHEMA = "visio_schema.v1.sensor.ImuQuat"
TOPIC = "/ego/imu/0/quat"
MS = 1_000_000


def _bundle(offsets=(0, 2 * MS, 4 * MS)):
    bundle = ImuQuat()
    # Deliberately a different clock from the received header.
    bundle.first_sample_time.FromNanoseconds(10_000)
    for i, offset in enumerate(offsets):
        s = bundle.samples.add(t_offset_ns=offset)
        s.orientation.x, s.orientation.y = i + 0.1, i + 0.2
        s.orientation.z, s.orientation.w = i + 0.3, i + 0.4
    return bundle


def _row(anchor, offsets=(0, 2 * MS, 4 * MS), topic=TOPIC):
    channel = make_channel(topic, SCHEMA, stream_id=16)
    msg = Message(stream_id=16, payload=_bundle(offsets).SerializeToString())
    msg.timestamp.FromNanoseconds(anchor)
    return msg, channel


def _write(path, rows):
    with McapWriter(path) as writer:
        for row in rows:
            writer.write(*row)
    return path


def test_schema_resolves_with_transitive_descriptors():
    assert message_class(SCHEMA) is ImuQuat
    descriptor = descriptor_pb2.FileDescriptorSet.FromString(file_descriptor_set(SCHEMA))
    names = {file.name for file in descriptor.file}
    assert {"visio_schema/v1/sensor/imu_quat.proto", "foxglove/Quaternion.proto",
            "google/protobuf/timestamp.proto"} <= names


def test_adapter_preserves_values_and_separates_sample_and_arrival_clocks():
    ctx = AdapterContext(make_decoders=None, message_class_for=message_class)
    adapter = build_adapter(SCHEMA, ctx)
    triples = list(adapter.emit(_bundle().SerializeToString(), TOPIC, 100 * MS))
    assert [sort for sort, _, _ in triples] == [100 * MS, 102 * MS, 104 * MS]
    assert [arrival for _, arrival, _ in triples] == [100 * MS] * 3
    for i, (_, _, sample) in enumerate(triples):
        assert isinstance(sample, Orientation)
        assert sample.topic == TOPIC
        np.testing.assert_array_equal(sample.q, np.array([i + .1, i + .2, i + .3, i + .4]))
    # Reusing the parse buffer must not change previously yielded arrays.
    list(adapter.emit(_bundle((0,)).SerializeToString(), TOPIC, 200 * MS))
    assert triples[-1][2].q[0] == 2.1
    assert list(adapter.emit(ImuQuat().SerializeToString(), TOPIC, 300 * MS)) == []


def test_live_rows_and_recording_yield_identical_orientations(tmp_path):
    rows = [_row(100 * MS), _row(133 * MS, (0,))]
    path = _write(tmp_path / "quat.mcap", rows)
    live = list(elements(rows, reorder_ns=100 * MS))
    replay = list(elements(read_mcap(path), reorder_ns=100 * MS))
    session = list(Session.open(path).stream([TOPIC]))
    for actual in (replay, session):
        assert [sample.t_ns for sample in actual] == [sample.t_ns for sample in live]
        np.testing.assert_array_equal([s.q for s in actual], [s.q for s in live])


@pytest.mark.parametrize("raw", [False, True])
def test_window_keeps_old_shard_bundle_samples_on_the_new_shards_clock(tmp_path, raw):
    # The first file's last MESSAGE is at 100 ms, but its samples reach 2 s.
    # A window starting at 2 s must still read that file for decoded samples.
    early = _write(tmp_path / "early.mcap", [_row(100 * MS, (0, 1900 * MS, 1902 * MS))])
    late = _write(tmp_path / "late.mcap", [_row(2010 * MS, (0, 2 * MS))])
    session = Session([early, late])
    expected = [e for e in session.stream([TOPIC], raw=raw)
                if 2000 * MS <= e.t_ns < 2012 * MS]
    actual = list(session.stream([TOPIC], start_ns=2000 * MS,
                                 end_ns=2012 * MS, raw=raw))
    assert [s.t_ns for s in actual] == [s.t_ns for s in expected]
    if not raw:
        assert [s.t_ns for s in actual] == [2000 * MS, 2002 * MS, 2010 * MS]
