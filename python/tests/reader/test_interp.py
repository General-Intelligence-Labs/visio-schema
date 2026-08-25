"""`reader.interp` — the type-dispatched blend table.

The claims here are the ones a consumer cannot check for itself: that a rotation
takes the short arc, that a sign-flipped quaternion is the SAME rotation and not a
full turn away, that an absent joint is dropped rather than invented, and that a
type with no interpolator says so instead of guessing.
"""

from __future__ import annotations

import numpy as np
import pytest

from visio_schema.reader import Frame, JointState, Pose, Record, blend
from visio_schema.reader.interp import slerp_xyzw

T0, T1 = 1_000_000_000, 2_000_000_000


def _pose(t_ns, pos, quat, frame_id="odom", topic="/pose/left") -> Pose:
    return Pose(
        topic=topic,
        t_ns=t_ns,
        position=np.array(pos, float),
        orientation=np.array(quat, float),
        frame_id=frame_id,
    )


def _rot_z(deg: float) -> np.ndarray:
    half = np.deg2rad(deg) / 2
    return np.array([0.0, 0.0, np.sin(half), np.cos(half)])


# ── pose ───────────────────────────────────────────────────────────────── #


def test_pose_midpoint_lerps_position_and_slerps_rotation():
    lo = _pose(T0, (0, 0, 0), _rot_z(0))
    hi = _pose(T1, (2, 4, 6), _rot_z(90))
    mid = blend(lo, hi, (T0 + T1) // 2)

    assert mid.t_ns == (T0 + T1) // 2
    assert mid.position == pytest.approx([1, 2, 3])
    # Half of a 90-degree turn is 45 degrees, NOT the normalized average of the
    # two quaternions (which lands at ~45 deg here only because the arc is short —
    # the property that matters is the angle, so assert the angle).
    assert np.rad2deg(2 * np.arccos(np.clip(mid.orientation[3], -1, 1))) == pytest.approx(45.0)
    assert np.linalg.norm(mid.orientation) == pytest.approx(1.0)


def test_slerp_takes_the_short_arc_across_a_sign_flip():
    """``q`` and ``-q`` are the same rotation. Without the dot-sign fix a blend
    between them travels a full turn — a wrist that visibly spins the wrong way
    for one frame, from data that never moved."""
    q = _rot_z(10)
    mid = slerp_xyzw(q, -q, 0.5)
    # Same rotation at both ends => the midpoint is that rotation (up to sign).
    assert min(np.linalg.norm(mid - q), np.linalg.norm(mid + q)) == pytest.approx(0, abs=1e-9)


def test_slerp_is_stable_for_near_identical_rotations():
    """sin(theta) -> 0 as the two converge; the lerp branch must not divide by it."""
    a, b = _rot_z(0.0), _rot_z(1e-6)
    out = slerp_xyzw(a, b, 0.5)
    assert np.all(np.isfinite(out))
    assert np.linalg.norm(out) == pytest.approx(1.0)


def test_pose_extrapolates_past_the_bracket():
    """``w > 1`` is the same kernel — this is what makes the live 'no sample yet'
    path need no second implementation."""
    lo = _pose(T0, (0, 0, 0), _rot_z(0))
    hi = _pose(T1, (1, 0, 0), _rot_z(10))
    out = blend(lo, hi, T1 + (T1 - T0))  # w = 2
    assert out.position == pytest.approx([2, 0, 0])
    assert np.rad2deg(2 * np.arccos(np.clip(out.orientation[3], -1, 1))) == pytest.approx(20.0)


def test_pose_refuses_to_blend_across_frames():
    """A pose's frame is an identity, not a quantity: two poses in different
    frames describe different things and have no midpoint."""
    lo = _pose(T0, (0, 0, 0), _rot_z(0), frame_id="odom")
    hi = _pose(T1, (1, 1, 1), _rot_z(10), frame_id="base")
    with pytest.raises(ValueError, match="different things"):
        blend(lo, hi, (T0 + T1) // 2)


def test_two_samples_at_one_instant_are_refused_rather_than_picked():
    """Duplicate stamps carrying different values are a producer fault. Picking
    one is wrong half the time, and the caller that can reach this (`_past_end`
    projecting from the last two samples) would then label an unmodified older
    sample `extrapolated`."""
    lo = _pose(T0, (0, 0, 0), _rot_z(0))
    hi = _pose(T0, (9, 9, 9), _rot_z(90))
    with pytest.raises(ValueError, match="same instant"):
        blend(lo, hi, T0)


# ── joints ─────────────────────────────────────────────────────────────── #


def test_joint_state_lerps_shared_joints_and_drops_the_rest():
    """A joint present at only one end has nothing to blend against. Holding it
    would publish a value at a time nothing measured it, labelled interpolated."""
    lo = JointState("/gripper", T0, {"left": 0.0, "right": 1.0})
    hi = JointState("/gripper", T1, {"left": 1.0, "extra": 5.0})
    mid = blend(lo, hi, (T0 + T1) // 2)
    assert mid.positions == {"left": pytest.approx(0.5)}
    assert mid.t_ns == (T0 + T1) // 2


def test_joint_state_keeps_optional_maps_none_when_absent():
    lo = JointState("/gripper", T0, {"a": 0.0}, velocities={"a": 2.0})
    hi = JointState("/gripper", T1, {"a": 1.0}, velocities=None)
    mid = blend(lo, hi, (T0 + T1) // 2)
    assert mid.velocities is None  # one side has none => nothing to blend
    assert mid.efforts is None


# ── the deliberate non-entries ─────────────────────────────────────────── #


def test_frames_and_records_are_not_interpolable():
    """Not an omission. There is no image halfway between two images, and an
    opaque proto has no arithmetic — `sync` reports `nearest` for these, which is
    exactly what a None from `blend` selects."""
    img = np.zeros((2, 2, 3), np.uint8)
    a, b = Frame("/cam0", T0, img), Frame("/cam0", T1, img)
    assert blend(a, b, (T0 + T1) // 2) is None

    r0 = Record("/x", T0, "some.Schema", None)
    r1 = Record("/x", T1, "some.Schema", None)
    assert blend(r0, r1, (T0 + T1) // 2) is None


def test_mixed_element_kinds_on_one_topic_are_refused():
    """One topic bracketed by two different kinds means its schema changed
    mid-session, or an adapter override reached one file and not another. That
    must not degrade to `nearest`, which is indistinguishable from a camera."""
    lo = _pose(T0, (0, 0, 0), _rot_z(0))
    hi = JointState("/pose/left", T1, {"a": 1.0})
    with pytest.raises(TypeError, match="changed element kind"):
        blend(lo, hi, (T0 + T1) // 2)


def test_a_sentinel_quaternion_is_refused_not_normalized_into_a_pose():
    """The bug this guard exists for. Several producers write an all-zero
    quaternion for "no pose here". Slerping it against a real rotation
    renormalizes the result to unit length, so a sentinel goes in and a plausible
    rotation comes out — and a downstream validity check of the usual shape
    (`norm > 0.5`) then passes on a pose nothing measured."""
    real = _pose(T1, (1, 1, 1), _rot_z(45))
    sentinel = _pose(T0, (0, 0, 0), np.zeros(4))
    with pytest.raises(ValueError, match="not a rotation"):
        blend(sentinel, real, (T0 + T1) // 2)
