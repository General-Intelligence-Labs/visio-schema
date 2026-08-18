"""`visio_schema.build` — foxglove payload builders.

Each builder makes a claim in its docstring that a consumer relies on and that
nothing else asserts: the quaternion order, the mono16 geometry, which fields are
omitted when absent, and that a format the reader cannot decode is refused at the
write end rather than an hour into the read.
"""

from __future__ import annotations

import numpy as np
import pytest

from visio_schema import build
from visio_schema.reader import decodable_formats
from visio_schema.reader.domain import CameraCalib

T0 = 1_700_000_000_123_456_789


def test_every_builder_round_trips_the_timestamp_exactly():
    """A derived message joins its source by EXACT timestamp equality, so a lost
    nanosecond here silently unpairs it from the frame it was computed from."""
    made = [
        build.raw_image_mono16(T0, np.zeros((2, 3), np.uint16)),
        build.compressed_video(T0, b"au"),
        build.compressed_image(T0, b"jpg"),
        build.pose_in_frame(T0, (0, 0, 0), (0, 0, 0, 1), frame_id="odom"),
        build.frame_transform(T0, parent_frame_id="a", child_frame_id="b",
                              translation_m=(0, 0, 0), rotation_xyzw=(0, 0, 0, 1)),
        build.joint_states(T0, {"left": 0.0}),
    ]
    for msg in made:
        assert msg.timestamp.ToNanoseconds() == T0, type(msg).__name__


# ── mono16 geometry ────────────────────────────────────────────────────── #

def test_raw_image_mono16_geometry_matches_its_payload():
    depth = np.arange(12, dtype=np.uint16).reshape(3, 4)   # h=3, w=4
    msg = build.raw_image_mono16(T0, depth, frame_id="rect")
    assert (msg.width, msg.height) == (4, 3)
    assert msg.encoding == "mono16"
    assert msg.step == 4 * 2
    assert len(msg.data) == msg.step * msg.height
    assert np.frombuffer(msg.data, "<u2").tolist() == depth.reshape(-1).tolist()
    assert msg.frame_id == "rect"


def test_raw_image_mono16_is_little_endian_regardless_of_input_order():
    """`mono16` specifies little-endian; a big-endian input must be converted,
    not passed through with the declared encoding lying about it."""
    be = np.array([[0x0102]], dtype=">u2")
    assert build.raw_image_mono16(T0, be).data == b"\x02\x01"


def test_raw_image_mono16_refuses_a_multichannel_array():
    """`step = width*2` describes ONE channel. An (H,W,3) array would declare a
    geometry three times smaller than the payload it ships, and a consumer
    reading step*height decodes garbage from a message nothing rejected."""
    with pytest.raises(ValueError, match="2-D single-channel"):
        build.raw_image_mono16(T0, np.zeros((4, 6, 3), np.uint16))


# ── quaternion order ───────────────────────────────────────────────────── #

def test_pose_in_frame_keeps_xyzw_order():
    """(x,y,z,w) — foxglove's order and scipy's `as_quat()`. In-house NPZs store
    wxyz; a silent swap here rotates every pose and nothing raises."""
    msg = build.pose_in_frame(T0, (1.0, 2.0, 3.0), (0.1, 0.2, 0.3, 0.9),
                              frame_id="odom")
    assert (msg.pose.position.x, msg.pose.position.y, msg.pose.position.z) == (1, 2, 3)
    o = msg.pose.orientation
    assert (o.x, o.y, o.z, o.w) == pytest.approx((0.1, 0.2, 0.3, 0.9))
    assert msg.frame_id == "odom"


def test_pose_in_frame_requires_a_frame_id():
    """No default: a pose without the frame it is expressed in is meaningless,
    and a silent default publishes every pose in the wrong one."""
    with pytest.raises(TypeError):
        build.pose_in_frame(T0, (0, 0, 0), (0, 0, 0, 1))


def test_frame_transform_keeps_xyzw_and_the_parent_child_direction():
    msg = build.frame_transform(
        T0, parent_frame_id="world", child_frame_id="imu",
        translation_m=(1.0, 2.0, 3.0), rotation_xyzw=(0.1, 0.2, 0.3, 0.9),
    )
    assert (msg.parent_frame_id, msg.child_frame_id) == ("world", "imu")
    assert (msg.translation.x, msg.translation.y, msg.translation.z) == (1, 2, 3)
    r = msg.rotation
    assert (r.x, r.y, r.z, r.w) == pytest.approx((0.1, 0.2, 0.3, 0.9))


# ── format gating ──────────────────────────────────────────────────────── #

def test_compressed_image_refuses_a_format_the_reader_cannot_decode():
    """The write side is the cheaper boundary: it knows at the first message."""
    with pytest.raises(ValueError, match="png"):
        build.compressed_image(T0, b"\x89PNG", fmt="png")


def test_compressed_video_refuses_a_format_the_reader_cannot_decode():
    """Same failure mode as the still builder, so the same guard — a sidecar in a
    codec with no decoder here dies on the first read otherwise."""
    with pytest.raises(ValueError, match="vp9"):
        build.compressed_video(T0, b"\x00", fmt="vp9")


def test_stills_are_gated_tighter_than_video():
    """A still must be SELF-CONTAINED, so only the mjpeg family. Video accepts
    anything the decoder handles — motion-JPEG included, since that is a real
    video codec and the reader decodes it. Both sets come from the decoder's own
    table, so neither can drift from what the read side will accept."""
    stills = decodable_formats("mjpeg")
    assert stills == {"jpeg", "mjpeg"}

    for fmt in decodable_formats():
        build.compressed_video(T0, b"x", fmt=fmt)          # video: all of them
    for fmt in stills:
        build.compressed_image(T0, b"x", fmt=fmt)          # stills: only these
    for fmt in decodable_formats() - stills:
        with pytest.raises(ValueError):
            build.compressed_image(T0, b"x", fmt=fmt)      # inter-coded: refused


def test_compressed_builders_carry_data_and_format_verbatim():
    msg = build.compressed_video(T0, b"annexb", frame_id="rect", fmt="h265")
    assert (msg.data, msg.format, msg.frame_id) == (b"annexb", "h265", "rect")


# ── calibration ────────────────────────────────────────────────────────── #

def _calib(**kw) -> CameraCalib:
    base = dict(
        topic="camera/1", width=640, height=480, model="fisheye",
        K=np.arange(9, dtype=float).reshape(3, 3),
        D=np.array([0.1, 0.2, 0.3, 0.4]), frame_id="cam1", R=None, P=None,
    )
    base.update(kw)
    return CameraCalib(**base)


def test_camera_calibration_flattens_row_major():
    msg = build.camera_calibration(T0, _calib())
    assert (msg.width, msg.height) == (640, 480)
    assert msg.distortion_model == "fisheye"
    assert msg.frame_id == "cam1"
    assert list(msg.K) == list(range(9))          # row-major, not transposed
    assert list(msg.D) == pytest.approx([0.1, 0.2, 0.3, 0.4])


def test_camera_calibration_omits_absent_r_and_p_rather_than_zero_filling():
    """A zero R is a legal (degenerate) rectification, so an absent one must be
    absent — a consumer cannot otherwise tell 'not rectified' from 'rectified to
    zero'."""
    bare = build.camera_calibration(T0, _calib())
    assert len(bare.R) == 0 and len(bare.P) == 0

    full = build.camera_calibration(T0, _calib(
        R=np.eye(3), P=np.arange(12, dtype=float).reshape(3, 4)))
    assert list(full.R) == [1, 0, 0, 0, 1, 0, 0, 0, 1]
    assert list(full.P) == list(range(12))


# ── joint states ───────────────────────────────────────────────────────── #

def _read_back(msg):
    """The message through the reader's own adapter — the round trip that matters."""
    from visio_schema import Message, make_channel
    from visio_schema.reader import elements

    channel = make_channel("/gripper", build.JOINT_STATES, stream_id=16)
    row = Message(stream_id=16, payload=msg.SerializeToString())
    row.timestamp.FromNanoseconds(T0)
    (el,) = list(elements([(row, channel)]))
    return el


def test_joint_states_round_trips_through_the_reader_adapter():
    el = _read_back(build.joint_states(T0, {"left": 0.25, "right": 0.75}))
    assert el.positions == {"left": 0.25, "right": 0.75}
    assert el.velocities is None and el.efforts is None
    assert el.t_ns == T0


def test_joint_states_leaves_an_absent_field_unset_rather_than_zero():
    """`position` is `optional double`, and the adapter uses `HasField` to tell a
    closed gripper at 0.0 from a producer that published no width. A builder that
    wrote a default would collapse the two on the channel a policy acts on."""
    msg = build.joint_states(T0, {}, velocities={"wrist": 1.5})

    (joint,) = msg.joints
    assert joint.name == "wrist"
    assert not joint.HasField("position")
    assert joint.HasField("velocity")
    assert _read_back(msg).positions == {}


def test_joint_states_keeps_a_real_zero():
    msg = build.joint_states(T0, {"left": 0.0})
    assert msg.joints[0].HasField("position")
    assert _read_back(msg).positions == {"left": 0.0}


def test_joint_states_carries_velocity_and_effort_per_joint():
    el = _read_back(build.joint_states(
        T0, {"a": 1.0, "b": 2.0},
        velocities={"a": 0.5}, efforts={"b": -3.0},
    ))
    assert el.positions == {"a": 1.0, "b": 2.0}
    assert el.velocities == {"a": 0.5}
    assert el.efforts == {"b": -3.0}


def test_joint_states_joint_order_follows_positions_then_the_extras():
    """Order is not semantic — the reader keys by name — but the bytes are stable."""
    msg = build.joint_states(
        T0, {"b": 1.0, "a": 2.0}, velocities={"a": 0.0, "z": 9.0}
    )
    assert [j.name for j in msg.joints] == ["b", "a", "z"]
