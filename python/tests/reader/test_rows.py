"""`reader.elements` — rows to elements, off any row source.

The claim under test is that a live loop and a replay of that loop's own recording
decode identically, so the two can be compared group for group. `Session` reads mcap
chunks its own way; `elements` reads the `(Message, Channel)` rows `read_mcap` and
`read_serial` yield, and the bus pops. Both must produce the same elements.
"""

from __future__ import annotations

import numpy as np
import pytest
from google.protobuf import descriptor_pb2

pytest.importorskip("av")
pytest.importorskip("mcap")

from _helpers import (
    FRAME_DT,
    QUAT,
    T0,
    VIDEO,
    RecBuilder,
    encode_jpeg,
    frame_index_of,
    indexed_frames,
)

from visio_schema import Message, make_channel, read_mcap
from visio_schema.reader import (
    Frame,
    ImuSample,
    JointState,
    Pose,
    Record,
    Session,
    elements,
    resolve_message_class,
)
from visio_schema.routing import Channel

MS = 1_000_000


def _row(topic: str, schema_name: str, t_ns: int, payload: bytes, *, seq: int = 0):
    """One ``(Message, Channel)`` row, the shape every row source yields."""
    channel = make_channel(topic, schema_name, stream_id=16)
    msg = Message(stream_id=channel.id, payload=payload, seq=seq)
    msg.timestamp.FromNanoseconds(t_ns)
    return msg, channel


def _rows_of(path):
    return list(read_mcap(path))


def _key(el):
    return (el.topic, el.t_ns, type(el).__name__)


# ── the parity claim ───────────────────────────────────────────────────── #


def test_elements_over_read_mcap_matches_session_stream(tmp_path):
    """One recording, two entry points, the same elements.

    Membership rather than order: `reorder_ns` is what decides ordering, and the two
    callers are entitled to different lateness budgets. What must not differ is WHICH
    elements exist and what they are stamped with.
    """
    path = tmp_path / "rec.mcap"
    b = RecBuilder(path)
    b.add_camera("/cam0", indexed_frames(6))
    b.add_imu_bundle("/imu", T0, [0, 5 * MS, 10 * MS])
    b.add_imu_bundle("/imu", T0 + 100 * MS, [0, 5 * MS])
    b.add_poses("/pose/left", n=6)
    b.write()

    topics = ["/cam0", "/imu", "/pose/left"]
    via_session = sorted(_key(e) for e in Session(path).stream(topics))
    via_rows = sorted(_key(e) for e in elements(_rows_of(path)))

    assert via_rows == via_session
    assert via_session, "fixture produced no elements — the comparison is vacuous"


def test_frames_carry_their_own_message_stamp_and_pixels(tmp_path):
    path = tmp_path / "rec.mcap"
    RecBuilder(path).add_camera("/cam0", indexed_frames(5)).write()

    frames = [e for e in elements(_rows_of(path)) if isinstance(e, Frame)]

    assert [f.t_ns for f in frames] == [T0 + i * FRAME_DT for i in range(5)]
    assert [frame_index_of(f.image) for f in frames] == list(range(5))


def test_imu_bundle_expands_onto_its_own_sample_clock(tmp_path):
    path = tmp_path / "rec.mcap"
    RecBuilder(path).add_imu_bundle("/imu", T0, [0, 5 * MS, 10 * MS]).write()

    samples = [e for e in elements(_rows_of(path)) if isinstance(e, ImuSample)]

    assert [s.t_ns for s in samples] == [T0, T0 + 5 * MS, T0 + 10 * MS]


def test_pose_and_joint_states_arrive_typed(tmp_path):
    """Not `Record`: `interp` dispatches on element type, so a pose that arrives
    opaque cannot be interpolated at all."""
    path = tmp_path / "rec.mcap"
    b = RecBuilder(path)
    b.add_poses("/pose/left", n=3)
    b.add_joint_states("/gripper", n=3)
    b.write()

    got = list(elements(_rows_of(path)))

    assert [type(e) for e in got if e.topic == "/pose/left"] == [Pose] * 3
    assert [type(e) for e in got if e.topic == "/gripper"] == [JointState] * 3


def test_unregistered_schema_falls_back_to_record(tmp_path):
    path = tmp_path / "rec.mcap"
    RecBuilder(path).add_quat("/imu/quat", n=3).write()

    got = list(elements(_rows_of(path)))

    assert [type(e) for e in got] == [Record] * 3
    assert {e.schema_name for e in got} == {QUAT}


# ── the live shape: MJPEG straight off the camera ──────────────────────── #


def test_mjpeg_compressed_video_decodes_without_a_re_encode():
    """The robot's live camera path: the UVC device's own MJPG buffer goes on the
    wire as `CompressedVideo` and is decoded once, here. Serving and training then
    decode the identical bytes rather than differing by a codec generation."""
    from visio_schema import build

    originals = indexed_frames(4)
    rows = []
    for i, img in enumerate(originals):
        data, _ = encode_jpeg(img)
        t = T0 + i * FRAME_DT
        payload = build.compressed_video(
            t, data, frame_id="cam_high", fmt="mjpeg"
        ).SerializeToString()
        rows.append(_row("/cam_high", VIDEO, t, payload, seq=i))

    frames = list(elements(rows))

    assert [type(f) for f in frames] == [Frame] * 4
    assert [f.t_ns for f in frames] == [T0 + i * FRAME_DT for i in range(4)]
    assert [frame_index_of(f.image) for f in frames] == list(range(4))
    assert all(f.image.shape == originals[0].shape for f in frames)
    assert all(f.frame_id == "cam_high" for f in frames)


def test_two_camera_topics_get_independent_decoders():
    """Interleaved on one stream, so a single shared decoder would feed each camera
    the other's reference frames."""
    from visio_schema import build

    rows = []
    for i, img in enumerate(indexed_frames(4)):
        data, _ = encode_jpeg(img)
        t = T0 + i * FRAME_DT
        for topic in ("/cam_high", "/cam_low"):
            payload = build.compressed_video(t, data, fmt="mjpeg").SerializeToString()
            rows.append(_row(topic, VIDEO, t, payload, seq=i))

    frames = list(elements(rows))

    for topic in ("/cam_high", "/cam_low"):
        got = [f for f in frames if f.topic == topic]
        assert [frame_index_of(f.image) for f in got] == list(range(4))


# ── ordering ───────────────────────────────────────────────────────────── #


def _pose_rows(topic, stamps):
    from visio_schema import build

    out = []
    for i, t in enumerate(stamps):
        m = build.pose_in_frame(
            t, np.zeros(3), np.array([0.0, 0, 0, 1.0]), frame_id="odom"
        )
        out.append(_row(topic, m.DESCRIPTOR.full_name, t, m.SerializeToString(), seq=i))
    return out


def test_reorder_budget_sorts_late_arrivals():
    """Cross-topic rows arrive out of order by construction on a bus; `reorder_ns`
    is the lateness budget that buys the sort back."""
    a = _pose_rows("/a", [T0, T0 + 20 * MS, T0 + 40 * MS])
    b = _pose_rows("/b", [T0 + 10 * MS, T0 + 30 * MS])
    interleaved = [a[0], a[1], b[0], a[2], b[1]]  # /b lags /a by one sample

    late = [e.t_ns for e in elements(interleaved, reorder_ns=0)]
    sorted_out = [e.t_ns for e in elements(interleaved, reorder_ns=25 * MS)]

    assert late != sorted(late), "reorder_ns=0 must not silently sort"
    assert sorted_out == sorted(sorted_out)


def test_zero_budget_still_yields_every_element():
    """Late is late, never dropped — the tail drains in order at end of stream."""
    a = _pose_rows("/a", [T0, T0 + 20 * MS])
    b = _pose_rows("/b", [T0 + 10 * MS])

    got = list(elements([a[0], a[1], b[0]], reorder_ns=0))

    assert sorted(e.t_ns for e in got) == [T0, T0 + 10 * MS, T0 + 20 * MS]


def test_adapters_are_built_lazily_on_first_sight_of_a_schema():
    """A live stream has no index to enumerate schemas from, so a topic that only
    starts publishing halfway through still has to decode."""
    from visio_schema import build

    rows = _pose_rows("/early", [T0 + i * MS for i in range(5)])
    data, _ = encode_jpeg(indexed_frames(1)[0])
    t = T0 + 5 * MS
    rows.append(
        _row("/late_cam", VIDEO, t,
             build.compressed_video(t, data, fmt="mjpeg").SerializeToString())
    )

    got = list(elements(rows))

    assert sum(isinstance(e, Frame) for e in got) == 1


# ── self-describing schemas ────────────────────────────────────────────── #


def _foreign_descriptor_set(full_name: str) -> bytes:
    """A `FileDescriptorSet` for a type this process has no generated module for."""
    package, _, name = full_name.rpartition(".")
    fds = descriptor_pb2.FileDescriptorSet()
    f = fds.file.add()
    f.name = "test_rows/foreign.proto"
    f.package = package
    f.syntax = "proto3"
    m = f.message_type.add()
    m.name = name
    field = m.field.add()
    field.name = "value"
    field.number = 1
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    return fds.SerializeToString()


FOREIGN = "test_rows.foreign.Foreign"


def test_resolve_message_class_prefers_the_generated_class():
    from visio_schema import message_class

    got = resolve_message_class(
        QUAT, encoding="protobuf", descriptor_set=_foreign_descriptor_set(FOREIGN)
    )

    assert got is message_class(QUAT)


def test_resolve_message_class_falls_back_to_the_embedded_descriptor_set():
    cls = resolve_message_class(
        FOREIGN, encoding="protobuf",
        descriptor_set=_foreign_descriptor_set(FOREIGN),
    )

    m = cls(value=7)
    assert cls().FromString(m.SerializeToString()).value == 7


def test_resolve_message_class_refuses_a_non_protobuf_encoding():
    with pytest.raises(ValueError, match="not\n?\\s*protobuf|is not protobuf"):
        resolve_message_class(
            FOREIGN, encoding="jsonschema", descriptor_set=b"", where="/derived"
        )


def test_a_channel_carrying_its_own_schema_reads_as_a_record():
    """What makes a self-describing channel pay off: a derived stream whose
    generated module this process never imported still decodes."""
    fds = _foreign_descriptor_set(FOREIGN)
    cls = resolve_message_class(FOREIGN, encoding="protobuf", descriptor_set=fds)
    channel = Channel(
        id=16, topic="/derived", encoding="protobuf", schema_name=FOREIGN,
        schema=fds, schema_encoding="protobuf",
    )
    msg = Message(stream_id=16, payload=cls(value=42).SerializeToString())
    msg.timestamp.FromNanoseconds(T0)

    got = list(elements([(msg, channel)]))

    assert [type(e) for e in got] == [Record]
    assert got[0].msg.value == 42
    assert got[0].t_ns == T0
