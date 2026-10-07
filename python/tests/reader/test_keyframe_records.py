"""Indexed compressed sampling: pixels/exposure never enter the read path."""

import numpy as np
import pytest
from _helpers import FRAME_DT, T0, indexed_frames
from mcap.writer import IndexType, Writer

from visio_schema import make_channel, message_class
from visio_schema.reader import HevcDecoder, Session, is_keyframe

CAM = "/ego/camera/1"


def test_window_seeks_and_never_decodes_or_loads_exposure(rec, monkeypatch):
    builder = rec(device="GILABS-test")
    builder.add_camera(CAM, indexed_frames(20) * 5, keyint=5)
    builder.add_frame_info(CAM, n=100)
    path = builder.write(chunk_size=400)
    session = Session.open(path, require_index=True)

    def forbidden(*args, **kwargs):
        pytest.fail("sparse record read decoded pixels or loaded exposure")

    monkeypatch.setattr(HevcDecoder, "decode", forbidden)
    monkeypatch.setattr(Session, "_exposure_tracks", forbidden)
    rows = list(session.keyframe_records(
        [CAM], start_ns=T0 + 80 * FRAME_DT, end_ns=T0 + 86 * FRAME_DT))
    assert [r.t_ns for r in rows] == [T0 + 80 * FRAME_DT, T0 + 85 * FRAME_DT]
    for row in rows:
        assert row.topic == CAM
        message = message_class(row.schema_name)()
        message.ParseFromString(row.data)
        assert is_keyframe(message.format, message.data)
    assert session.sparse_read_stats["bytes_read"] < path.stat().st_size


def test_sparse_pixels_equal_full_decode(rec):
    b = rec().add_camera(CAM, indexed_frames(20), keyint=5)
    session = Session(b.write())
    full = {f.t_ns: f.image for f in session.stream([CAM], gray=True)}
    rows = session.keyframe_records(
        [CAM], start_ns=T0 + 10 * FRAME_DT, end_ns=T0 + 16 * FRAME_DT)
    for row in rows:
        message = message_class(row.schema_name)()
        message.ParseFromString(row.data)
        image = HevcDecoder(message.format, pixel_format="gray").decode(message.data)
        np.testing.assert_array_equal(image, full[row.t_ns])


def test_duplicate_seams_are_deduplicated_and_conflicts_rejected(rec):
    a = rec("a.mcap").add_camera(CAM, indexed_frames(10), keyint=5)
    b = rec("b.mcap")
    b._rows = list(a._rows[-5:])
    session = Session([a.write(), b.write()])
    assert len(list(session.keyframe_records(
        [CAM], start_ns=T0, end_ns=T0 + 10 * FRAME_DT))) == 2
    b._rows[-5] = (*b._rows[-5][:3], b._rows[-5][3] + b"\x98\x06\x01")
    session = Session([a.path, b.write()])
    with pytest.raises(ValueError, match="conflicting keyframes"):
        list(session.keyframe_records(
            [CAM], start_ns=T0, end_ns=T0 + 10 * FRAME_DT))


def test_no_chunk_index_refused_before_message_read(rec):
    builder = rec().add_camera(CAM, indexed_frames(2))
    channel = make_channel(CAM, "foxglove.CompressedVideo", stream_id=0)
    with builder.path.open("wb") as file:
        writer = Writer(file, index_types=IndexType.MESSAGE)
        writer.start()
        schema = writer.register_schema(name=channel.schema_name,
                                        encoding="protobuf", data=channel.schema)
        cid = writer.register_channel(topic=CAM, message_encoding="protobuf",
                                      schema_id=schema)
        for _, _, stamp, data in builder._rows:
            writer.add_message(channel_id=cid, log_time=stamp,
                               publish_time=stamp, data=data)
        writer.finish()
    session = Session.open(builder.path, require_index=True)
    with pytest.raises(ValueError, match="chunk indexes"):
        session.require_chunk_indexes()
    with pytest.raises(ValueError, match="chunk indexes"):
        session.keyframe_records([CAM], start_ns=T0, end_ns=T0 + 10 * FRAME_DT)


def test_unsupported_codec_never_falls_back_to_full_decode(rec):
    path = rec().add_camera(CAM, indexed_frames(2), fmt="av1").write()
    session = Session.open(path, require_index=True)
    with pytest.raises(ValueError, match="unsupported sparse video codec"):
        list(session.keyframe_records(
            [CAM], start_ns=T0, end_ns=T0 + 10 * FRAME_DT))


def test_missing_summary_refused_at_open(rec):
    path = rec().add_camera(CAM, indexed_frames(2)).write()
    path.write_bytes(path.read_bytes()[:-30])
    with pytest.raises(ValueError, match="no MCAP summary"):
        Session.open(path, require_index=True)


@pytest.mark.parametrize("topics,start,end", [([], 0, 1), ([CAM], 1, 1), (CAM, 0, 1)])
def test_invalid_queries(rec, topics, start, end):
    session = Session(rec().add_camera(CAM, indexed_frames(2)).write())
    with pytest.raises(ValueError):
        session.keyframe_records(topics, start_ns=start, end_ns=end)
