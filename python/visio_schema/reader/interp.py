"""Blend two elements of the same kind — the type-dispatched interpolation table.

The read side has two registries and they key on different things, deliberately:

    adapters   schema name  ->  how to BUILD an element from bytes
    interp     element type ->  how to BLEND two elements at a time between them

Interpolation cannot be a property of the op, because "the value between these
two" means something different per kind. A position lerps; a quaternion slerps
(and lerping one is simply wrong); a *decoded image* has no between at all. So
`sync` consults this table for each of its ``resample`` keys, and a kind with no
entry is honestly reported as `nearest` rather than silently averaged into
something that never existed.

**Not registering is a decision, not an omission.** `Frame` and `Record` are
absent because they cannot be blended. `ImuSample` is absent because it *could*
be and usually should not: a consumer asking for IMU wants the samples the device
produced, and a lerped one is invented data wearing a real sample's type. Anyone
who wants it registers it themselves — the table is open.

The precedent is `_ExposureTrack._lerp_exposure`, which already does this for one
type: blend what varies continuously, take the nearer neighbour for what does not
(a fractional VTS register is meaningless rather than approximate), and flag the
result so a consumer can tell. Same rules here, generalized.

``w`` is NOT clamped to [0, 1]. ``w > 1`` is extrapolation past ``hi`` — the same
kernel, and the reason `sync(on_future="extrapolate")` needs no second
implementation. `sync` labels which regime produced a value; this layer just
evaluates the formula.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import numpy as np

from .domain import Element, JointState, Ns, Pose

# (lo, hi, w) -> an element of the same type, stamped at the target time.
Interpolator = Callable[[Element, Element, float, Ns], Element]

_REGISTRY: dict[type, Interpolator] = {}


def interpolator(cls: type) -> Callable[[Interpolator], Interpolator]:
    """Register how to blend two elements of type ``cls``."""

    def register(fn: Interpolator) -> Interpolator:
        if cls in _REGISTRY:
            raise ValueError(f"interpolator already registered for {cls.__name__}")
        _REGISTRY[cls] = fn
        return fn

    return register


def blend(lo: Element, hi: Element, t_ns: Ns) -> Element | None:
    """``lo`` and ``hi`` evaluated at ``t_ns``; None if the type has no blend.

    ``t_ns`` outside ``[lo.t_ns, hi.t_ns]`` extrapolates — see the module
    docstring. Returning None rather than raising is what lets `sync` fall back to
    `nearest` for a `Frame` without the caller pre-checking every type, and is why
    there is no public "is this interpolable" predicate: the one place that asks
    also wants the answer, so a separate query would only invite a race between
    the two.

    None means exactly ONE thing — this kind has no interpolator. The two ways a
    call can be malformed raise instead, because `sync` renders a None as
    `method="nearest"`, which is indistinguishable from a camera and would bury
    them:

    * **Mixed types on one topic.** Two different element kinds bracketing a
      single topic means its schema changed mid-session, or an adapter override
      was applied to one file and not another. `_blend_pose` already raises on a
      `frame_id` change, a strictly milder inconsistency.
    * **A zero span.** Two samples at one instant carrying different values is a
      producer fault; picking `lo` is the wrong answer half the time, and the one
      caller that can reach it (`_past_end`, projecting from the last two) would
      then label an unmodified older sample `extrapolated`.
    """
    if type(hi) is not type(lo):
        raise TypeError(
            f"cannot blend {type(lo).__name__} with {type(hi).__name__} on "
            f"{lo.topic!r}: one topic changed element kind mid-stream"
        )
    fn = _REGISTRY.get(type(lo))
    if fn is None:
        return None
    span = hi.t_ns - lo.t_ns
    if span == 0:
        raise ValueError(
            f"cannot blend two samples of {lo.topic!r} at the same instant "
            f"({lo.t_ns}) — duplicate stamps are a producer fault, and either "
            "answer here is wrong half the time"
        )
    return fn(lo, hi, (t_ns - lo.t_ns) / span, t_ns)


# ── rotation ───────────────────────────────────────────────────────────── #


def slerp_xyzw(q0: np.ndarray, q1: np.ndarray, w: float) -> np.ndarray:
    """Spherical linear interpolation of unit quaternions, ``(x, y, z, w)``.

    Hand-rolled rather than `scipy.spatial.transform.Slerp` for two reasons that
    are both requirements here: Slerp refuses times outside its key range, which
    is exactly the extrapolation case; and it builds a spline object per call
    pair, which is wasted on a two-key blend in a per-tick loop.

    Two correctness details that a lerp does not have:

    * **Sign.** ``q`` and ``-q`` are the same rotation, so a raw blend between a
      quaternion and its own negation takes the 360-degree path. Negating ``q1``
      when the dot is negative picks the short arc — without it a pose stream that
      happens to cross the sign boundary spins the wrong way through a full turn.
    * **Near-parallel.** As the dot approaches 1 the ``sin(theta)`` denominator
      goes to zero; below the angle where the two differ meaningfully, a
      normalized lerp is both stable and accurate to well under float precision.
    """
    q0 = np.asarray(q0, float)
    q1 = np.asarray(q1, float)
    # BOTH inputs must already be rotations. A non-unit quaternion is not one, and
    # the all-zero "no pose here" sentinel that several producers write is the
    # common case: blending it against a real rotation normalizes the result back
    # to unit length, so a sentinel goes IN and a plausible rotation comes OUT.
    # A downstream validity check of the usual shape (`norm > 0.5`) then passes
    # on a pose that was never measured. Refuse instead
    # — an absent sample is `missing`, not something to interpolate through.
    for name, q in (("lo", q0), ("hi", q1)):
        norm = float(np.linalg.norm(q))
        if abs(norm - 1.0) > 1e-3:
            raise ValueError(
                f"slerp_xyzw: {name} quaternion has norm {norm:.6g}, not 1 — "
                "that is not a rotation. An all-zero sentinel means the producer "
                "had no pose; interpolating it would manufacture one."
            )
    dot = float(np.dot(q0, q1))
    if dot < 0.0:
        q1, dot = -q1, -dot
    dot = min(dot, 1.0)
    if dot > 0.9995:  # near-parallel: sin(theta) -> 0, lerp instead
        out = q0 + w * (q1 - q0)
    else:
        theta = np.arccos(dot)
        s = np.sin(theta)
        out = (np.sin((1.0 - w) * theta) * q0 + np.sin(w * theta) * q1) / s
    return out / np.linalg.norm(out)


# ── the built-in kinds ─────────────────────────────────────────────────── #


@interpolator(Pose)
def _blend_pose(lo: Pose, hi: Pose, w: float, t_ns: Ns) -> Pose:
    """Lerp the position, slerp the orientation.

    ``frame_id`` is taken from ``lo``, not blended: it is an identifier. Two poses
    in *different* frames have no meaningful midpoint, so a change across a bracket
    is a producer bug — raised here rather than silently reported in whichever
    frame happened to sort first.
    """
    if lo.frame_id != hi.frame_id:
        raise ValueError(
            f"cannot interpolate {lo.topic!r} across frames "
            f"{lo.frame_id!r} -> {hi.frame_id!r}: a pose's frame is an identity, "
            "not a quantity, so the two samples describe different things"
        )
    return replace(
        lo,
        t_ns=t_ns,
        position=lo.position + w * (hi.position - lo.position),
        orientation=slerp_xyzw(lo.orientation, hi.orientation, w),
    )


@interpolator(JointState)
def _blend_joint_state(
    lo: JointState, hi: JointState, w: float, t_ns: Ns
) -> JointState:
    """Lerp per joint, over the joints BOTH samples carry.

    A joint in only one end of the bracket is dropped rather than held: holding it
    would emit a value the caller reads as interpolated, at a timestamp where
    nothing measured it. Dropping surfaces as an absent key, which a caller already
    has to handle for a joint the producer never published.

    Acceleration is not carried by this element (`JointState` models position /
    velocity / effort), so there is nothing here that must NOT be blended — every
    field is a continuous physical quantity.
    """
    return replace(
        lo,
        t_ns=t_ns,
        positions=_lerp_shared(lo.positions, hi.positions, w),
        velocities=_lerp_optional(lo.velocities, hi.velocities, w),
        efforts=_lerp_optional(lo.efforts, hi.efforts, w),
    )


def _lerp_shared(
    lo: dict[str, float], hi: dict[str, float], w: float
) -> dict[str, float]:
    """Blend the joints BOTH ends carry. Never None — `positions` is not optional,
    and a helper that could return one would write it into that field unchecked."""
    return {k: lo[k] + w * (hi[k] - lo[k]) for k in lo.keys() & hi.keys()}


def _lerp_optional(
    lo: dict[str, float] | None, hi: dict[str, float] | None, w: float
) -> dict[str, float] | None:
    return None if lo is None or hi is None else _lerp_shared(lo, hi, w)
