"""`sync` with resampled keys — one op for matched and interpolated streams.

The contract this file exists to pin: **an offline call and a live one differ only
in their keys and their lag budget.** If they could drift, a tolerance tuned on
recordings would say nothing about the control loop, and the train/serve agreement
the whole design rests on would be a coincidence.
"""

from __future__ import annotations

import numpy as np
import pytest
from _helpers import FRAME_DT, RecBuilder, indexed_frames

from visio_schema.reader import (
    Frame,
    JointState,
    Pose,
    Session,
    SyncGroup,
    Tick,
    sync,
)

MS = 1_000_000
T0 = 1_700_000_000_000_000_000
POSE = "/pose/left"


def _pose(t_ns, x, topic=POSE) -> Pose:
    return Pose(
        topic=topic,
        t_ns=t_ns,
        position=np.array([x, 0.0, 0.0]),
        orientation=np.array([0.0, 0.0, 0.0, 1.0]),
        frame_id="odom",
    )


def _frame(t_ns, topic="/cam0") -> Frame:
    return Frame(topic, t_ns, np.zeros((2, 2, 3), np.uint8))


def _stream(*els):
    return sorted(els, key=lambda e: e.t_ns)


def _one(els, **kw):
    kw.setdefault("tol_ns", 0)
    (group,) = list(sync(els, ["/cam0"], **kw))
    return group


# ── a key is matched or resampled, and says which ──────────────────────── #


def test_matched_keys_are_carried_whole_and_labelled_matched():
    """A decoded frame is never blended — there is no image between two images —
    so a match key always yields the real element, whatever `mode` says."""
    a, b = _frame(T0, "/cam0"), _frame(T0 + 2 * MS, "/cam1")
    (g,) = list(sync([a, b], ["/cam0", "/cam1"], tol_ns=10 * MS))
    assert g["/cam0"] is a and g["/cam1"] is b
    assert {s.method for s in g.by_topic.values()} == {"matched"}
    assert g.t_ns == T0  # the earliest member


def test_resampled_keys_are_evaluated_at_the_group_instant():
    """The headline case: a 100 Hz pose evaluated at a 30 Hz frame's instant."""
    els = _stream(_pose(T0, 0.0), _frame(T0 + 5 * MS), _pose(T0 + 10 * MS, 1.0))
    g = _one(els, resample={POSE: "interpolate"}, lag_ns=50 * MS)

    got = g.by_topic[POSE]
    assert got.method == "interpolated"
    assert got.element.position[0] == pytest.approx(0.5)
    assert got.element.t_ns == T0 + 5 * MS  # stamped at the INSTANT, not the source
    assert got.residual_ns == 5 * MS  # distance to the closer bracket end
    assert g.complete


def test_residual_of_a_matched_only_group_is_still_the_pairwise_spread():
    """`residual_ns` was redefined from 'max pairwise spread' to 'worst distance
    to the group time' so one number covers matched AND resampled keys. Because
    the group time is the earliest member, those are the same value for a matched
    set — so no existing consumer's number moved."""
    els = [_frame(T0, "/cam0"), _frame(T0 + 7 * MS, "/cam1")]
    (g,) = list(sync(els, ["/cam0", "/cam1"], tol_ns=10 * MS))
    assert g.residual_ns == 7 * MS


def test_residual_spans_matched_and_resampled_keys():
    els = _stream(
        _frame(T0, "/cam0"), _frame(T0 + 2 * MS, "/cam1"),
        _pose(T0 - 30 * MS, 1.0),
    )
    (g,) = list(sync(els, ["/cam0", "/cam1"], resample={POSE: "interpolate"}, tol_ns=10 * MS))
    assert g.by_topic[POSE].method == "held"
    assert g.residual_ns == 30 * MS  # the stale pose, not the 2 ms stereo spread


# ── the lag budget, the one real knob ──────────────────────────────────── #


def test_zero_lag_holds_and_labels_it():
    """`lag_ns=0` never waits, so the future half of the bracket is never
    available: the value is the last one before, which is a one-sided lag. The
    label is what makes that visible instead of assumed."""
    els = _stream(_pose(T0, 0.0), _frame(T0 + 5 * MS), _pose(T0 + 10 * MS, 1.0))
    got = _one(els, resample={POSE: "interpolate"}, lag_ns=0).by_topic[POSE]
    assert got.method == "held"
    assert got.element.position[0] == pytest.approx(0.0)
    assert got.residual_ns == 5 * MS


def test_exact_hit_short_circuits():
    els = _stream(_pose(T0, 7.0), _frame(T0), _pose(T0 + 10 * MS, 9.0))
    got = _one(els, resample={POSE: "interpolate"}, lag_ns=50 * MS).by_topic[POSE]
    assert (got.method, got.residual_ns) == ("exact", 0)
    assert got.element.position[0] == pytest.approx(7.0)


def test_uninterpolable_resampled_types_fall_back_to_nearest():
    """A `Frame` on a resample key cannot be blended — so it is picked, and
    `method` says which of the two it is."""
    els = _stream(
        _frame(T0, "/cam1"), _frame(T0 + 6 * MS, "/cam0"), _frame(T0 + 10 * MS, "/cam1")
    )
    got = _one(els, resample={"/cam1": "interpolate"}, lag_ns=50 * MS).by_topic["/cam1"]
    assert got.method == "nearest"
    assert got.element.t_ns == T0 + 10 * MS  # 4 ms away beats 6 ms
    assert got.residual_ns == 4 * MS


def test_end_of_stream_closes_pending_groups_rather_than_dropping_them():
    """The last instants have no future sample and never will; they must still
    come out, held, not vanish because the lag never elapsed."""
    els = _stream(_pose(T0, 1.0), _frame(T0 + 5 * MS), _frame(T0 + 9 * MS))
    groups = list(sync(els, ["/cam0"], resample={POSE: "interpolate"},
                       tol_ns=0, lag_ns=10_000 * MS))
    assert [g.t_ns for g in groups] == [T0 + 5 * MS, T0 + 9 * MS]
    assert {g.by_topic[POSE].method for g in groups} == {"held"}


def test_output_order_follows_the_match_stream():
    els = _stream(*[_frame(T0 + i * 10 * MS) for i in range(5)], _pose(T0, 0.0))
    groups = list(sync(els, ["/cam0"], resample={POSE: "interpolate"}, tol_ns=0, lag_ns=15 * MS))
    assert [g.t_ns for g in groups] == [T0 + i * 10 * MS for i in range(5)]


# ── missing: absent, or too stale ──────────────────────────────────────── #


def test_a_topic_that_never_published_is_missing_not_absent():
    els = _stream(_pose(T0, 0.0), _frame(T0 + 5 * MS))
    g = _one(els, resample=dict.fromkeys([POSE, "/gripper"], "interpolate"), lag_ns=0)
    assert g.missing == ("/gripper",)
    assert not g.complete
    assert POSE in g


def test_stale_ns_moves_a_stale_value_into_missing():
    """The one thing a hand-rolled hold-last cannot do: make staleness a
    reportable outcome rather than an invisible one."""
    els = _stream(_pose(T0, 0.0), _frame(T0 + 500 * MS))
    loose = _one(els, resample={POSE: "interpolate"}, lag_ns=0)
    strict = _one(els, resample={POSE: "interpolate"}, lag_ns=0, stale_ns=20 * MS)

    assert loose.by_topic[POSE].residual_ns == 500 * MS  # present, stale
    assert strict.missing == (POSE,)  # same data, rejected


def test_head_of_stream_reports_a_large_residual_rather_than_silence():
    """An instant BEFORE the first sample has no causal value at all. visio_data's
    `hold_last_indices` clips the index to 0 here and silently back-extrapolates;
    this reports `nearest` with the true distance, so `stale_ns` can reject it."""
    els = _stream(_frame(T0), _pose(T0 + 40 * MS, 3.0))
    got = _one(els, resample={POSE: "interpolate"}, lag_ns=50 * MS).by_topic[POSE]
    assert (got.method, got.residual_ns) == ("nearest", 40 * MS)


def test_a_key_may_not_be_both_matched_and_resampled():
    with pytest.raises(ValueError, match="never both"):
        list(sync([], ["/cam0"], resample={"/cam0": "interpolate"}, tol_ns=0))


# ── plain sync still behaves exactly as before ─────────────────────────── #


def test_plain_sync_drops_unmatched_and_passes_through():
    """No resample keys, default lag: byte-for-byte the old behaviour, because
    every existing consumer calls it that way."""
    els = _stream(
        _frame(T0, "/cam0"), _frame(T0 + 1 * MS, "/cam1"),
        _frame(T0 + 100 * MS, "/cam0"),  # partner never arrives
        _pose(T0 + 2 * MS, 0.0),
    )
    out = list(sync(els, ["/cam0", "/cam1"], tol_ns=5 * MS, passthrough=True))
    groups = [x for x in out if isinstance(x, SyncGroup)]
    passed = [x for x in out if not isinstance(x, SyncGroup)]
    assert len(groups) == 1 and groups[0].t_ns == T0
    assert [p.topic for p in passed] == [POSE]


def test_emit_partial_still_yields_dropped_singletons():
    els = _stream(_frame(T0, "/cam0"), _frame(T0 + 500 * MS, "/cam1"))
    out = list(sync(els, ["/cam0", "/cam1"], tol_ns=1 * MS,
                    on_incomplete="emit_partial"))
    assert any(isinstance(x, Frame) for x in out)


# ── bounded memory ─────────────────────────────────────────────────────── #


def test_buffers_stay_bounded_by_the_lag_budget_not_the_recording():
    """The rule the whole reader rests on: state is proportional to the lag,
    never to the stream length. A decoded 1080p `Frame` is ~6.2 MB, so an
    unbounded pending queue here is an OOM on a real recording, not a slow leak."""
    from visio_schema.reader.ops import _Resampler

    r = _Resampler({POSE: "interpolate"}, on_future="hold", stale_ns=None)
    for i in range(10_000):
        r.push(_pose(T0 + i * MS, float(i)))
        r.prune_before(T0 + i * MS - 20 * MS)  # a 20 ms lag budget
    assert len(r._buf[POSE]) <= 25  # ~one budget + the kept history

    poses = [_pose(T0 + i * MS, float(i)) for i in range(5000)]
    # Off-grid (+0.5 ms) so every instant lands strictly BETWEEN two poses:
    # on-grid ones would come back `exact` and prove nothing about the bracket.
    frames = [_frame(T0 + i * 10 * MS + MS // 2) for i in range(499)]
    groups = list(sync(_stream(*poses, *frames), ["/cam0"],
                       resample={POSE: "interpolate"}, tol_ns=0, lag_ns=20 * MS))
    assert len(groups) == 499
    assert all(g.by_topic[POSE].method == "interpolated" for g in groups)


def test_prune_keeps_enough_history_to_extrapolate():
    """`_Resampler.KEEP_BEFORE = 2` is load-bearing: pruning to a single sample
    would make `on_future="extrapolate"` silently degrade to `held` on a long
    stream — correct-looking output, quietly worse."""
    from visio_schema.reader.ops import _Resampler

    r = _Resampler({POSE: "interpolate"}, on_future="extrapolate",
                   stale_ns=None)
    for i in range(100):
        r.push(_pose(T0 + i * MS, float(i)))
        r.prune_before(T0 + i * MS)
    values, _ = r.at(T0 + 110 * MS)
    assert values[POSE].method == "extrapolated"


# ── the live loop: same call, ticks for the instants ───────────────────── #


def _live(elements, *, lag_ns, tick_dt, n_ticks, t0=T0 + MS // 2, **kw):
    """A live stream: the loop's ticks merged with the incoming elements.

    Online, `stream` is a queue fed by the wire reader AND by the loop's own
    clock. Here that merge is just a sort, which is what a queue drained in
    arrival order gives you.

    Ticks default to half a millisecond off the sample grid so none lands ON a
    sample: an `exact` hit would agree trivially and prove nothing about the blend.
    """
    ticks = [Tick("/tick", t0 + i * tick_dt) for i in range(n_ticks)]
    merged = sorted([*elements, *ticks], key=lambda e: e.t_ns)
    return list(sync(merged, ["/tick"], tol_ns=0, lag_ns=lag_ns, **kw))


def test_a_control_loop_is_the_same_call_with_ticks_as_the_match_key():
    """No pull cursor, no second API: the loop pushes `Tick`s, and its own clock
    both defines the instants and advances the watermark that releases them."""
    poses = [_pose(T0 + i * 10 * MS, float(i)) for i in range(40)]
    groups = _live(poses, lag_ns=20 * MS, tick_dt=25 * MS, n_ticks=12,
                   resample={POSE: "interpolate"})

    assert len(groups) == 12
    assert all(isinstance(g.by_topic["/tick"].element, Tick) for g in groups)
    assert {g.by_topic[POSE].method for g in groups} == {"interpolated"}


def test_lag_zero_is_the_minimum_latency_choice_and_says_so():
    poses = [_pose(T0 + i * 10 * MS, float(i)) for i in range(40)]
    groups = _live(poses, lag_ns=0, tick_dt=25 * MS, n_ticks=12, resample={POSE: "interpolate"})
    assert {g.by_topic[POSE].method for g in groups} == {"held"}


def test_offline_and_live_agree_on_value_method_and_residual():
    """THE contract, and the reason there is one op. Same samples, same instants:
    identical values, identical labels, identical residuals. So a `stale_ns` tuned
    offline IS the live gate."""
    poses = [_pose(T0 + i * 10 * MS, float(i)) for i in range(300)]
    instants = [T0 + 5 * MS + MS // 2 + i * 17 * MS for i in range(120)]

    offline = list(
        sync(_stream(*poses, *[_frame(t) for t in instants]), ["/cam0"],
             resample={POSE: "interpolate"}, tol_ns=0, lag_ns=100 * MS)
    )
    live = list(
        sync(sorted([*poses, *[Tick("/tick", t) for t in instants]],
                    key=lambda e: e.t_ns),
             ["/tick"], resample={POSE: "interpolate"}, tol_ns=0, lag_ns=100 * MS)
    )

    assert [g.t_ns for g in offline] == [g.t_ns for g in live] == instants
    for a, b in zip(offline, live, strict=True):
        sa, sb = a.by_topic[POSE], b.by_topic[POSE]
        assert (sa.method, sa.residual_ns) == (sb.method, sb.residual_ns)
        assert sa.element.position == pytest.approx(sb.element.position)
    assert {a.by_topic[POSE].method for a in offline} == {"interpolated"}


def test_extrapolation_is_opt_in():
    poses = [_pose(T0, 0.0), _pose(T0 + 10 * MS, 1.0)]
    ahead = [Tick("/tick", T0 + 20 * MS)]

    (held,) = list(sync([*poses, *ahead], ["/tick"], resample={POSE: "interpolate"}, tol_ns=0))
    assert held.by_topic[POSE].method == "held"

    (proj,) = list(sync([*poses, *ahead], ["/tick"], resample={POSE: "interpolate"}, tol_ns=0,
                        on_future="extrapolate"))
    assert proj.by_topic[POSE].method == "extrapolated"
    assert proj[POSE].position[0] == pytest.approx(2.0)


def test_extrapolation_needs_two_samples():
    els = [_pose(T0, 4.0), Tick("/tick", T0 + 10 * MS)]
    (g,) = list(sync(els, ["/tick"], resample={POSE: "interpolate"}, tol_ns=0,
                     on_future="extrapolate"))
    assert g.by_topic[POSE].method == "held"  # no slope -> the causal choice


def test_out_of_order_arrivals_are_ordered_before_bracketing():
    """Online the stream is a merge of independent producers — a wire reader, a
    local camera, the loop's ticks — which genuinely interleave."""
    els = [_pose(T0 + 20 * MS, 2.0), _pose(T0, 0.0), Tick("/tick", T0 + 10 * MS)]
    (g,) = list(sync(els, ["/tick"], resample={POSE: "interpolate"}, tol_ns=0, lag_ns=0))
    assert g.by_topic[POSE].method == "interpolated"
    assert g[POSE].position[0] == pytest.approx(1.0)


# ── through a real MCAP ────────────────────────────────────────────────── #


def test_stereo_matched_with_trajectories_resampled_onto_it(tmp_path):
    """The visio-data shape in one call: the stereo pair matched, the wrist pose
    and gripper resampled onto its instants.

    Poses run at 3x the frame rate and are offset by half a pose period, so every
    instant falls strictly inside a bracket — the arrangement a device actually
    produces, and the one where hold-last and interpolate differ.
    """
    pose_dt = FRAME_DT // 3
    rec = tmp_path / "rec.mcap"
    (
        RecBuilder(rec, device="ego")
        .add_camera("/camera/0", indexed_frames(10))
        .add_camera("/camera/1", indexed_frames(10), skew=2 * MS)
        .add_poses("/vio/pose", n=40, t0=T0 - pose_dt // 2, dt=pose_dt,
                   step=(0.1, 0, 0))
        .add_joint_states("/joint_states", n=40, t0=T0 - pose_dt // 2, dt=pose_dt)
        .write()
    )
    session = Session(rec, device="ego")
    topics = ["/camera/0", "/camera/1", "/vio/pose", "/joint_states"]

    groups = list(
        sync(
            session.stream(topics),
            ["/camera/0", "/camera/1"],
            resample=dict.fromkeys(["/vio/pose", "/joint_states"], "interpolate"),
            tol_ns=10 * MS,
            lag_ns=200 * MS,
            stale_ns=FRAME_DT,
        )
    )

    assert len(groups) == 10
    assert all(g.complete for g in groups)
    for g in groups:
        assert isinstance(g["/camera/0"], Frame)
        assert g.by_topic["/camera/1"].method == "matched"
        assert g.by_topic["/camera/1"].residual_ns == 2 * MS
        assert g.by_topic["/vio/pose"].method == "interpolated"

    # The pose track ramps 0.1 m per sample and each instant sits half a pose
    # period in, so x is a bracket midpoint — a value NO source sample carries.
    xs = [g["/vio/pose"].position[0] for g in groups]
    assert xs == pytest.approx([0.05 + 0.3 * i for i in range(10)])

    assert isinstance(groups[0]["/joint_states"], JointState)
    assert set(groups[0]["/joint_states"].positions) == {"left", "right"}
    # `velocity` was never published: absent, not 0.0.
    assert groups[0]["/joint_states"].velocities is None


# ── free-running cameras: one grid, the rest nearest ───────────────────── #


def test_one_primary_camera_with_the_rest_resampled_nearest():
    """The robot rig's shape: independent USB captures with no shared trigger.

    Matching them all would drop constantly — they never fire together. So one is
    the grid and the others are resampled, where they take the `nearest` fallback
    (an image cannot be blended) and each reports how far off it was. This is also
    exactly what visio_data's offline ingest already does by hand: reference
    camera, `nearest_indices` for the others.
    """
    primary = [_frame(T0 + i * 33 * MS, "/cam_high") for i in range(4)]
    # A second camera free-running at a different phase AND a different rate.
    wrist = [_frame(T0 + 7 * MS + i * 40 * MS, "/cam_wrist") for i in range(4)]

    groups = list(sync(_stream(*primary, *wrist), ["/cam_high"],
                       resample={"/cam_wrist": "nearest"}, tol_ns=0, lag_ns=100 * MS))

    assert [g.t_ns for g in groups] == [p.t_ns for p in primary]
    assert all(g.by_topic["/cam_high"].method == "matched" for g in groups)
    assert all(g.by_topic["/cam_wrist"].method == "nearest" for g in groups)
    # Every wrist frame is a REAL frame, never a blend, and never invented.
    assert all(g["/cam_wrist"] in wrist for g in groups)
    # And the phase error is reported rather than hidden.
    # primary at 0/33/66/99 ms against wrist at 7/47/87/127 ms: the distance to
    # the closer neighbour, which drifts as the two rates beat against each other.
    assert [g.by_topic["/cam_wrist"].residual_ns for g in groups] == [
        7 * MS, 14 * MS, 19 * MS, 12 * MS
    ]


def test_a_desynced_matched_pair_drops_where_nearest_would_paper_over_it():
    """Why a hardware-synced pair belongs in `match` and not in `resample`: a
    trigger that slips is a fault, and matching turns it into a visible drop
    instead of a quietly worse `nearest`."""
    good = [_frame(T0, "/cam0"), _frame(T0 + 1 * MS, "/cam1")]
    slipped = [_frame(T0 + 33 * MS, "/cam0"), _frame(T0 + 90 * MS, "/cam1")]

    matched = list(sync(_stream(*good, *slipped), ["/cam0", "/cam1"],
                        tol_ns=5 * MS))
    assert [g.t_ns for g in matched] == [T0]  # the slipped pair is refused

    resampled = list(sync(_stream(*good, *slipped), ["/cam0"],
                          resample={"/cam1": "interpolate"}, tol_ns=0, lag_ns=200 * MS))
    assert len(resampled) == 2  # both survive...
    assert resampled[1].by_topic["/cam1"].residual_ns == 32 * MS  # ...but say so


# ── per-key resample modes ─────────────────────────────────────────────── #


def _joints(t_ns, v, topic="/gripper") -> JointState:
    return JointState(topic, t_ns, {"left": v})


def test_each_resample_key_can_take_its_own_mode():
    """The visio-data shape, which needs all three at once: a second camera can
    only be PICKED, a measured trajectory should be BLENDED, and a commanded
    setpoint must be HELD."""
    els = _stream(
        _frame(T0 + 10 * MS, "/cam0"),                    # the grid
        _frame(T0, "/cam1"), _frame(T0 + 30 * MS, "/cam1"),
        _pose(T0, 0.0, "/pose/left"), _pose(T0 + 20 * MS, 2.0, "/pose/left"),
        _pose(T0, 9.0, "/action/left"), _pose(T0 + 20 * MS, 5.0, "/action/left"),
    )
    (g,) = list(sync(els, ["/cam0"], tol_ns=0, lag_ns=100 * MS, resample={
        "/cam1": "nearest",
        "/pose/left": "interpolate",
        "/action/left": "hold_last",
    }))

    assert g.by_topic["/cam1"].method == "nearest"
    assert g["/cam1"].t_ns == T0                       # 10 ms beats 20 ms

    assert g.by_topic["/pose/left"].method == "interpolated"
    assert g["/pose/left"].position[0] == pytest.approx(1.0)   # the midpoint

    assert g.by_topic["/action/left"].method == "held"
    assert g["/action/left"].position[0] == pytest.approx(9.0)  # never blended


def test_a_command_is_never_interpolated_even_where_a_pose_is():
    """The distinction the mapping exists for. A command is a zero-order hold by
    construction — the robot held that setpoint until the next one arrived — so
    blending it would publish a setpoint that was never sent. Same element type,
    same timestamps, different answer, because the SIGNAL differs."""
    els = _stream(
        _frame(T0 + 5 * MS, "/cam0"),
        _pose(T0, 0.0, "/measured"), _pose(T0 + 10 * MS, 1.0, "/measured"),
        _pose(T0, 0.0, "/commanded"), _pose(T0 + 10 * MS, 1.0, "/commanded"),
    )
    (g,) = list(sync(els, ["/cam0"], tol_ns=0, lag_ns=50 * MS,
                     resample={"/measured": "interpolate",
                               "/commanded": "hold_last"}))
    assert g["/measured"].position[0] == pytest.approx(0.5)
    assert g["/commanded"].position[0] == pytest.approx(0.0)


def test_bulk_same_mode_is_spelled_explicitly():
    """There is no "all of these, one mode" shorthand. It existed, with the mode
    defaulting to `interpolate`, which made `resample=["/action/x"]` quietly blend
    a commanded setpoint — the one error the op most wants to prevent, reachable
    only through the convenient spelling."""
    els = _stream(
        _frame(T0 + 5 * MS, "/cam0"),
        _pose(T0, 0.0, "/a"), _pose(T0 + 10 * MS, 1.0, "/a"),
        _pose(T0, 0.0, "/b"), _pose(T0 + 10 * MS, 1.0, "/b"),
    )
    (g,) = list(sync(els, ["/cam0"], tol_ns=0, lag_ns=50 * MS,
                     resample=dict.fromkeys(["/a", "/b"], "hold_last")))
    assert {s.method for k, s in g.by_topic.items() if k != "/cam0"} == {"held"}


def test_a_mapping_key_may_not_also_be_matched():
    with pytest.raises(ValueError, match="never both"):
        list(sync([], ["/cam0"], tol_ns=0, resample={"/cam0": "nearest"}))


def test_nearest_prefers_the_earlier_sample_on_an_exact_tie():
    """Inherited from visio_data's `nearest_indices`, whose `<=` picked the
    earlier sample when a grid time sat exactly between two. Ported here because
    that op is gone and this one replaced it: a silent flip would move a whole
    dataset's frame-to-sample assignment by one, with nothing to notice it."""
    els = _stream(
        _pose(T0, 0.0), _pose(T0 + 20 * MS, 2.0), _frame(T0 + 10 * MS),
    )
    got = _one(els, resample={POSE: "nearest"}, lag_ns=100 * MS).by_topic[POSE]
    assert got.element.position[0] == pytest.approx(0.0)  # the earlier one


def test_hold_last_before_the_first_sample_yields_that_first_sample():
    """Also inherited: `hold_last_indices` clipped its index to 0, so a grid time
    preceding every sample took sample 0. Same element here — but reported as
    `nearest` with the true distance rather than as a clean hold, so `stale_ns`
    can reject what used to be a silent back-extrapolation."""
    els = _stream(_frame(T0), _pose(T0 + 40 * MS, 3.0), _pose(T0 + 80 * MS, 7.0))
    got = _one(els, resample={POSE: "hold_last"}, lag_ns=200 * MS).by_topic[POSE]
    assert got.element.position[0] == pytest.approx(3.0)  # the first sample
    assert (got.method, got.residual_ns) == ("nearest", 40 * MS)


def test_match_skew_does_not_prune_history_a_later_group_still_needs():
    """A match key held back waiting for its partner becomes a group instant
    LATER, at a timestamp well behind the watermark. Pruning resampled history to
    the watermark threw those samples away, so the group fell back to the first
    that survived — a value from the FUTURE, labelled `nearest`, biased by exactly
    the inter-camera skew. At `lag_ns=0` it corrupted every group.

    The stereo skew here (20 ms) exceeds the pose period (5 ms), which is what
    makes the difference observable; a fixture whose skew is under one period
    hides it.
    """
    skew, dt = 20 * MS, 33 * MS
    els = []
    for i in range(6):
        els.append(_frame(T0 + i * dt, "/camA"))
        els.append(_frame(T0 + i * dt + skew, "/camB"))
    els += [_pose(T0 + k * 5 * MS, float(k)) for k in range(60)]

    groups = list(sync(_stream(*els), ["/camA", "/camB"], tol_ns=25 * MS,
                       resample={POSE: "interpolate"}, lag_ns=0))

    assert len(groups) == 6
    for g in groups:
        got = g.by_topic[POSE]
        # `nearest` IS the bug's signature: under these modes it can only arise
        # from `lo is None` — no sample at or before the instant — which here
        # means the one it needed had been pruned away. The value it falls back
        # to comes from the future and is biased by exactly the match skew.
        assert got.method != "nearest", f"pruned history at {g.t_ns - T0}"
        assert got.residual_ns < 5 * MS  # within one pose period of real data
