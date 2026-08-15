"""Layer 1 ops: sync (N-way match, drop, passthrough) + prefetch."""

from __future__ import annotations

import numpy as np
from _helpers import FRAME_DT, T0

from visio_schema.reader import Frame, ImuSample, SyncGroup, sync


def _frame(topic, t):
    return Frame(topic, t, np.zeros((2, 2, 3), np.uint8))


def _pose(topic, t):
    return ImuSample(topic, t, np.zeros(3), np.zeros(3))


def test_sync_stereo_pairs():
    stream = []
    for i in range(4):
        t = T0 + i * FRAME_DT
        stream.append(_frame("/c0", t))
        stream.append(_frame("/c1", t + 1_000_000))  # 1 ms inter-cam skew
    groups = list(sync(stream, ["/c0", "/c1"], tol_ns=12_000_000))
    assert len(groups) == 4
    assert all(isinstance(g, SyncGroup) for g in groups)
    assert all(g.residual_ns == 1_000_000 for g in groups)
    assert all(set(g.by_topic) == {"/c0", "/c1"} for g in groups)


def test_sync_drops_unmatched_partner():
    stream = [
        _frame("/c0", T0),
        _frame("/c1", T0 + 1_000_000),
        _frame("/c0", T0 + FRAME_DT),  # partnerless
        _frame("/c0", T0 + 2 * FRAME_DT),
        _frame("/c1", T0 + 2 * FRAME_DT + 1_000_000),
    ]
    groups = list(sync(stream, ["/c0", "/c1"], tol_ns=12_000_000))
    assert [g.t_ns for g in groups] == [T0, T0 + 2 * FRAME_DT]


def test_sync_passthrough_imu_inline():
    stream = [
        _frame("/c0", T0),
        _pose("/imu", T0 + 1_000_000),
        _frame("/c1", T0 + 1_000_000),
    ]
    out = list(sync(stream, ["/c0", "/c1"], tol_ns=12_000_000, passthrough=True))
    kinds = [type(x).__name__ for x in out]
    assert "ImuSample" in kinds and "SyncGroup" in kinds


def test_sync_without_passthrough_drops_nonkeys():
    stream = [_frame("/c0", T0), _pose("/imu", T0), _frame("/c1", T0)]
    out = list(sync(stream, ["/c0", "/c1"], tol_ns=12_000_000))
    assert all(isinstance(x, SyncGroup) for x in out)
    assert len(out) == 1


def test_hold_last_resampling_is_a_sync_option():
    """What the removed standalone `resample` did, as the `hold_last`, zero-lag
    corner of `sync` — one op, so a caller cannot pick the alignment semantics and
    the grouping semantics independently and get them out of step."""
    stream = [
        _pose("/pose", T0),
        _frame("/c0", T0 + 1_000_000),
        _frame("/c0", T0 + 2_000_000),
        _pose("/pose", T0 + 3_000_000),
        _frame("/c0", T0 + 4_000_000),
    ]
    out = list(sync(stream, ["/c0"], resample={"/pose": "hold_last"}, tol_ns=0))
    assert len(out) == 3
    assert [g["/pose"].t_ns for g in out] == [T0, T0, T0 + 3_000_000]
    assert {g.by_topic["/pose"].method for g in out} == {"held"}


# --- prefetch (threaded overlap primitive) --------------------------------- #
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from visio_schema.reader import prefetch  # noqa: E402


def test_prefetch_preserves_order_and_all_items():
    src = range(50)
    assert list(prefetch(src, depth=4)) == list(range(50))


def test_prefetch_overlaps_producer_and_consumer():
    # A producer that sleeps per item runs ahead while the consumer sleeps too;
    # wall-clock is well under the serial sum if they truly overlap.
    def slow():
        for i in range(6):
            time.sleep(0.02)
            yield i

    t0 = time.perf_counter()
    out = []
    for x in prefetch(slow(), depth=6):
        time.sleep(0.02)
        out.append(x)
    elapsed = time.perf_counter() - t0
    assert out == list(range(6))
    assert elapsed < 6 * 0.04 * 0.8  # < 80% of the fully-serial time


def test_prefetch_propagates_exception_in_order():
    def boom():
        yield 0
        yield 1
        raise ValueError("kaboom")

    seen = []
    with pytest.raises(ValueError, match="kaboom"):
        for x in prefetch(boom(), depth=4):
            seen.append(x)
    assert seen == [0, 1]  # items before the error arrive first, in order


def test_prefetch_early_exit_joins_worker():
    def infinite():
        i = 0
        while True:
            yield i
            i += 1

    got = []
    for x in prefetch(infinite(), depth=2):
        got.append(x)
        if len(got) == 3:
            break  # closes the generator -> finally signals + joins the worker
    assert got == [0, 1, 2]
    time.sleep(0.05)
    assert not any(t.name == "prefetch" for t in threading.enumerate())


def test_prefetch_empty_source():
    assert list(prefetch([], depth=3)) == []


def test_prefetch_bounded_ahead_of_stalled_consumer():
    # The docstring's load-bearing claim: the worker blocks once `depth` items are
    # queued. A source that records what it produced + a consumer that pulls one
    # then stalls proves the queue is capped (not the whole source raced ahead).
    produced = []

    def counting():
        for i in range(50):
            produced.append(i)
            yield i

    it = prefetch(counting(), depth=2)
    assert next(it) == 0        # starts the worker and consumes one item
    time.sleep(0.15)            # worker fills the bounded queue, then blocks on put
    # 1 consumed + depth queued + 1 blocked on put; NOT all 50
    assert len(produced) <= 2 + 2
    assert list(it) == list(range(1, 50))  # drains the rest cleanly (no loss)
