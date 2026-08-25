"""Reshaping the one ``Element`` stream: `sync`, plus `prefetch` to overlap it.

There is **no public ``merge``**: the timestamp-ordered union of a session's
files is the Session's own job, so ``Session.stream(...)`` is already the single
merged stream. And there is exactly one reshaping op, because there is exactly one
question — **what was every stream doing at this instant?**

How a key answers it is a property of the key, not of the pipeline: a
hardware-triggered frame is *matched* (picked whole; there is no image between two
images), a trajectory is *resampled* (evaluated there, since no sample of it
exists at that time). Both are `sync` arguments and both land in one `SyncGroup`.
The same call serves a recording and a live control loop.

There is no sparse-sampling op either. Thinning a stream *after* decode is the one
reshaping that cannot pay for itself: decode is the cost, so a filter placed above
``Session`` has already spent everything it saves. That job belongs to the source,
and it lives there — ``Session.keyframe_stream``.
"""

from __future__ import annotations

import bisect
import queue
import threading
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, TypeVar

from .domain import Element, Ns, Sampled, SyncGroup
from .interp import blend

_T = TypeVar("_T")

# How a resampled key's value is produced, and what to do past its last sample.
ResampleMode = Literal["hold_last", "nearest", "interpolate"]
OnFuture = Literal["hold", "extrapolate"]


class _Err:
    """Terminal queue item wrapping a producer exception (re-raised in order)."""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


_DONE = object()  # terminal queue sentinel: source exhausted normally


def prefetch(source: Iterable[_T], depth: int = 3) -> Iterator[_T]:
    """Run ``source`` on a worker thread, buffering up to ``depth`` items ahead.

    A general overlap primitive: the upstream generator (decode → rectify …)
    advances on its own thread while the consumer works, so the two run
    concurrently instead of in lockstep — the validated front-end∥engine win.
    Order-preserving and bounded: the worker blocks once ``depth`` items are
    queued. Exceptions from ``source`` propagate to the consumer in order; if the
    consumer stops early the worker is signalled and joined, so no thread leaks.

    Pure thread/queue — no CUDA coupling. A device ``Frame`` carries its own
    ``event``; the *consumer* waits that event on its stream before touching
    device memory, so overlapping the Python objects here is always safe.
    """
    q: queue.Queue = queue.Queue(maxsize=max(1, depth))
    stop = threading.Event()
    worker = threading.Thread(
        target=_prefetch_worker, args=(source, q, stop), name="prefetch", daemon=True
    )
    worker.start()
    try:
        yield from _prefetch_consume(q)
    finally:
        # Consumer is leaving (normal end, break, or error): signal the worker and
        # drain so a blocked `q.put` can proceed to its stop check, then join.
        stop.set()
        _drain(q)
        worker.join(timeout=1.0)


def _prefetch_worker(source: Iterable, q: queue.Queue, stop: threading.Event) -> None:
    """Feed ``source`` into ``q``, ending with ``_DONE`` or an ``_Err`` sentinel."""
    try:
        for item in source:
            if stop.is_set():
                return  # consumer left; no sentinel needed
            q.put(item)  # blocks when full (backpressure); consumer drain unblocks
    except BaseException as exc:
        q.put(_Err(exc))
    else:
        q.put(_DONE)


def _prefetch_consume(q: queue.Queue) -> Iterator:
    """Yield queued items until the terminal sentinel; re-raise producer errors."""
    while True:
        item = q.get()
        if item is _DONE:
            return
        if isinstance(item, _Err):
            raise item.exc
        yield item


def _drain(q: queue.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return




# ── sync: every key's value at one instant ─────────────────────────────── #
#
# A key contributes to a group in one of two ways, and which one is a property of
# the DATA, not of where the pipeline runs:
#
#   match      the key's own elements define the instant, and are picked whole.
#              For anything that cannot be interpolated — a decoded frame, an
#              opaque Record — this is the only honest treatment, and for anything
#              hardware-triggered (a stereo pair) it is also the correct one.
#   resample   the key runs on its own asynchronous clock, so no element of it
#              exists at the group instant and a VALUE is computed there instead.
#              Trajectories: poses, joint states.
#
# ONLINE AND OFFLINE ARE THE SAME CALL. What differs is the lag budget, and
# sometimes not even the keys:
#
#   a vision policy      match=[primary camera] BOTH SIDES. The training grid is
#                        the camera, so serving on any other grid would align the
#                        policy's inputs differently from how they were baked —
#                        "same op" is a far weaker guarantee than "same grid".
#                        It also makes a repeated frame impossible by
#                        construction: no frame, no group.
#   a state-only loop    match=["/tick"] — teleop, an intervention controller, a
#                        recorder, a safety monitor. No image is in the loop, so
#                        the loop's own clock supplies the instants; the `Tick`s
#                        both define them AND advance the watermark that releases
#                        them, so a live stream self-drives with no pull API.
#
# Free-running cameras (independent USB captures, no shared trigger) do NOT all
# belong in `match`: matching them would drop constantly. Make ONE the match key
# and resample the rest — they take the `nearest` fallback, since images cannot be
# blended, and each reports how far off it was. Hardware-triggered cameras (an
# exposure-synced stereo pair) do belong in `match` together: it is tighter, and
# a desync then shows up as a residual or a drop instead of being papered over.
#
# `lag_ns` is the one real knob: it buys the FUTURE half of every resampled
# bracket. At 0 no group is held back, so a resampled key gets whatever had
# arrived by the time its group formed — usually `held`, a one-sided bias of up to
# one source period. Past one source period the same data comes back
# `interpolated`. (Skewed match keys form a group later than its own instant, so
# even at 0 some brackets are complete; the budget bounds the wait, it does not
# forbid a bracket that arrived for free.)


def sync(
    stream: Iterable[Element],
    match: Iterable[str],
    *,
    resample: Mapping[str, ResampleMode] | None = None,
    tol_ns: Ns,
    lag_ns: Ns = 0,
    on_future: OnFuture = "hold",
    stale_ns: Ns | None = None,
    window: int = 8,
    passthrough: bool = False,
    on_incomplete: Literal["drop", "emit_partial"] = "drop",
) -> Iterator[SyncGroup | Element]:
    """Group a stream's keys onto shared instants; yields `SyncGroup`.

    ``match`` keys define the instants: a group forms when every one of them has an
    element within ``tol_ns``, and those elements are carried whole. ``resample``
    keys are then EVALUATED at that instant through `interp`, because they run on
    their own clock and have no element there. Both arrive in ``by_topic`` with a
    `Sampled.method` saying which happened.

    ``resample`` is a **mapping**, never a bare list: the mode is stated per key
    because the right treatment is a property of the signal, and one pass
    routinely needs all three::

        resample={"/camera/1":   "nearest",      # no image lies between two images
                  "/pose/left":  "interpolate",  # a measured trajectory
                  "/action/left": "hold_last"}   # a COMMAND: a zero-order hold by
                                                 # construction — the robot held that
                                                 # setpoint until the next one, so
                                                 # blending invents one never sent

    There is deliberately no "all of these, one mode" shorthand. It existed, with
    ``mode`` defaulting to ``"interpolate"``, which made ``resample=["/action/x"]``
    quietly blend a commanded setpoint — the exact error the paragraph above warns
    against, reachable only through the convenient spelling. Write
    ``dict.fromkeys(topics, "hold_last")`` and say which you meant.
    ::

        # a hardware-synced stereo rig, trajectories resampled onto its instants
        for g in sync(session.stream(topics), ["/camera/0", "/camera/1"],
                      resample=["/pose/left", "/gripper"],
                      tol_ns=EXPOSURE_TOL_NS, lag_ns=100 * MS):
            frames = g["/camera/0"], g["/camera/1"]   # real, matched
            pose   = g["/pose/left"]                  # interpolated to g.t_ns

        # free-running cameras: one is the grid, the others are nearest-matched
        sync(stream, ["/cam_high"],
             resample=["/cam_left_wrist", "/pose/left", "/gripper"], ...)

        # a state-only loop: no image, so the loop's own Ticks are the grid
        for g in sync(live, ["/tick"], resample=["/pose/left", "/gripper"],
                      tol_ns=0, lag_ns=LAG_NS, stale_ns=BUDGET_NS):
            if not g.complete:
                hold(); continue
            act(g["/pose/left"], g["/gripper"])

    A recording and a live loop differ only in the lag, and for a vision policy
    not even in the keys. That is the point: a `stale_ns` tuned on recordings is
    the identical live gate, so train and serve cannot silently disagree about
    what "in sync" means.

    ``tol_ns`` bounds the MATCH spread (how far apart a stereo pair may fire);
    ``stale_ns`` bounds a RESAMPLED key's distance to real data. They measure
    different things, so they are separate numbers — a stereo pair matched at 1 ms
    and a pose accepted at 20 ms is an ordinary configuration. Exceeding either
    moves the key into ``missing`` rather than letting it ride.

    Bounded: ≤ ``window`` elements per match key, ≤ one ``lag_ns`` of groups
    pending, and `_Resampler.KEEP_BEFORE` + one lag budget of samples per resample
    key. A decoded 1080p `Frame` is ~6.2 MB, so that bound is load-bearing — size
    it, don't guess it.

    ``passthrough=True`` also yields non-key elements inline in arrival order
    (VIO's single feed). ``on_incomplete="emit_partial"`` yields match elements
    dropped for want of a partner, as bare elements.
    """
    matcher = _Matcher(
        match, tol_ns=tol_ns, window=window,
        emit_partial=on_incomplete == "emit_partial",
    )
    resampler = _Resampler(
        resample or {}, on_future=on_future, stale_ns=stale_ns
    )
    overlap = set(matcher.keys) & set(resampler.topics)
    if overlap:
        raise ValueError(
            f"{sorted(overlap)} named in both `match` and `resample`; a key either "
            "defines the instants or is evaluated at them, never both"
        )

    pending: deque[_Matched] = deque()
    resampling = bool(resampler.topics)
    watermark: Ns = 0
    for el in stream:
        watermark = max(watermark, el.t_ns)
        if matcher.wants(el.topic):
            yield from _absorb(matcher.push(el), pending)
        elif resampler.wants(el.topic):
            resampler.push(el)
        elif passthrough:
            yield el
        # Released inline rather than through a helper: this runs per ELEMENT and
        # almost always does nothing, so a generator built and torn down each time
        # dominated the pure-match path (measured 0.13 -> 0.50 us/element on a
        # 306k-element stereo+IMU stream; inlining and gating the prune below puts
        # it back to 0.19).
        until = watermark - lag_ns
        while pending and pending[0].t_ns <= until:
            yield resampler.close(pending.popleft())
        if resampling:
            resampler.prune_before(_frontier(pending, matcher, watermark))

    # End of stream: form whatever the buffers still hold, then close every group
    # regardless of lag — the samples it was waiting for are never coming.
    yield from _absorb(matcher.flush(), pending)
    while pending:
        yield resampler.close(pending.popleft())


def _absorb(
    outcomes: Iterator[_Matched | Element], pending: deque[_Matched]
) -> Iterator[Element]:
    """Route the matcher's output: instants wait for resampling, drops pass out.

    A matched set is not a group yet — its resampled keys are filled in `lag_ns`
    later — so it queues rather than being yielded. A singleton the matcher gave up
    on has nothing to wait for and leaves immediately.
    """
    for out in outcomes:
        if isinstance(out, _Matched):
            pending.append(out)
        else:
            yield out


def _frontier(
    pending: deque[_Matched], matcher: _Matcher, watermark: Ns
) -> Ns:
    """The earliest instant a group could still be closed at.

    No resampled sample older than this can be needed again, so it is what
    `prune_before` may discard below.

    The matcher's own buffers are part of it, and leaving them out was a bug: a
    match key held back waiting for its partner (a stereo pair with a real
    inter-camera skew, say) becomes a group instant LATER, at a timestamp well
    behind the watermark. Pruning to the watermark threw away the samples that
    instant needed, so every such group fell back to the first sample that
    survived — a value from the future, labelled `nearest`, biased by exactly the
    match skew. It corrupted every group at ``lag_ns=0`` and the leading ones
    otherwise.
    """
    if pending:
        return pending[0].t_ns
    oldest = matcher.oldest_ns()
    return watermark if oldest is None else oldest


@dataclass(frozen=True)
class _Matched:
    """A complete match set, before its resampled keys are known.

    Deliberately not a `SyncGroup`: one of those states what every key was doing,
    and this only knows the matched half. Handing out a half-filled group — even
    briefly, even internally — is how a consumer ends up reading `missing` before
    it means anything.
    """

    t_ns: Ns
    by_topic: dict[str, Sampled]


class _Matcher:
    """Match keys -> the instants a group forms at.

    Buffers ≤ ``window`` elements per key and emits a set once every key has one
    within ``tol_ns``; when the heads cannot agree, the oldest is dropped, because
    its partner is already gone. Holds no opinion about resampling.
    """

    __slots__ = ("_buf", "_emit_partial", "_tol_ns", "_window", "keys")

    def __init__(
        self,
        keys: Iterable[str],
        *,
        tol_ns: Ns,
        window: int,
        emit_partial: bool,
    ) -> None:
        self.keys = list(keys)
        self._buf: dict[str, deque[Element]] = {k: deque() for k in self.keys}
        self._tol_ns = tol_ns
        self._window = window
        self._emit_partial = emit_partial

    def wants(self, topic: str) -> bool:
        return topic in self._buf

    def oldest_ns(self) -> Ns | None:
        """Earliest buffered match element, or None when nothing is held.

        These are instants that have not formed a group yet but still will, so a
        resampler must keep history back to here — see `_frontier`.
        """
        heads = [buf[0].t_ns for buf in self._buf.values() if buf]
        return min(heads) if heads else None

    def push(self, el: Element) -> Iterator[_Matched | Element]:
        """Buffer one element, then form whatever that made possible."""
        buf = self._buf[el.topic]
        buf.append(el)
        if len(buf) > self._window:
            old = buf.popleft()
            if self._emit_partial:
                yield old
        yield from self.flush()

    def flush(self) -> Iterator[_Matched | Element]:
        """Every set formable from the current heads; stops when one key runs dry."""
        while all(self._buf[k] for k in self.keys):
            heads = {k: self._buf[k][0] for k in self.keys}
            ts = [e.t_ns for e in heads.values()]
            lo, hi = min(ts), max(ts)
            if hi - lo > self._tol_ns:
                oldest = min(self.keys, key=lambda k: self._buf[k][0].t_ns)
                dropped = self._buf[oldest].popleft()
                if self._emit_partial:
                    yield dropped
                continue
            for k in self.keys:
                self._buf[k].popleft()
            # The instant is the EARLIEST member, which is what makes a member's
            # own offset from it equal the set's spread — see `SyncGroup.residual_ns`.
            yield _Matched(
                lo, {k: Sampled(e, "matched", e.t_ns - lo) for k, e in heads.items()}
            )


class _Resampler:
    """Resample keys -> their value at an arbitrary instant.

    Bounded per-topic history plus the bracket search. This is the half that must
    behave identically whether the instants come from a camera or from a control
    loop's `Tick`, which is what makes "online and offline are the same call" a
    fact about the code rather than a claim about the API.
    """

    # Samples at or before the instant that a track retains: the bracket's `lo`,
    # plus one more so `on_future="extrapolate"` has a slope to work with.
    KEEP_BEFORE = 2

    __slots__ = ("_buf", "_mode", "_on_future", "_stale_ns", "topics")

    def __init__(
        self,
        topics: Mapping[str, ResampleMode],
        *,
        on_future: OnFuture,
        stale_ns: Ns | None,
    ) -> None:
        # Per key, always. Within one pass a camera can only be picked
        # (`nearest`), a measured trajectory should be blended (`interpolate`),
        # and a commanded setpoint must be held (`hold_last`).
        self.topics = tuple(topics)
        self._buf: dict[str, list[Element]] = {t: [] for t in self.topics}
        self._mode = dict(topics)
        self._on_future = on_future
        self._stale_ns = stale_ns

    def wants(self, topic: str) -> bool:
        return topic in self._buf

    def push(self, el: Element) -> None:
        """Append a sample. Out-of-order arrivals are inserted, not appended.

        `Session.stream` emits in `t_ns` order, so the check costs one comparison
        in the normal case. It is not defensive: online the stream is a merge of
        independent producers — a wire reader, a local camera, the loop's own ticks
        — which genuinely interleave, and a mis-sorted track would silently bracket
        an instant with the wrong pair.
        """
        buf = self._buf[el.topic]
        if buf and el.t_ns < buf[-1].t_ns:
            bisect.insort(buf, el, key=lambda e: e.t_ns)
        else:
            buf.append(el)

    def prune_before(self, t_ns: Ns) -> None:
        """Drop history no instant at or after ``t_ns`` can need.

        Keeps `KEEP_BEFORE` samples at or before ``t_ns`` and everything after, so
        memory is bounded by the lag budget rather than by the recording — the rule
        the whole reader is built on.
        """
        for buf in self._buf.values():
            drop = 0
            while (
                len(buf) - drop > self.KEEP_BEFORE
                and buf[drop + self.KEEP_BEFORE].t_ns <= t_ns
            ):
                drop += 1
            if drop:
                del buf[:drop]

    def close(self, matched: _Matched) -> SyncGroup:
        """Finish a matched set into a group by evaluating every resampled key."""
        values, missing = self.at(matched.t_ns)
        return SyncGroup(
            t_ns=matched.t_ns,
            by_topic={**matched.by_topic, **values},
            missing=missing,
        )

    def at(self, t_ns: Ns) -> tuple[dict[str, Sampled], tuple[str, ...]]:
        """Every resampled topic evaluated at ``t_ns`` -> (values, missing)."""
        values: dict[str, Sampled] = {}
        missing: list[str] = []
        for topic in self.topics:
            got = self._value_at(self._buf[topic], t_ns, self._mode[topic])
            if got is None or (
                self._stale_ns is not None and got.residual_ns > self._stale_ns
            ):
                missing.append(topic)
            else:
                values[topic] = got
        return values, tuple(missing)

    def _value_at(
        self, buf: list[Element], t_ns: Ns, mode: ResampleMode
    ) -> Sampled | None:
        if not buf:
            return None
        # Index of the first sample strictly after the instant; everything before
        # it is at or before. `key=` rather than materializing the timestamps: this
        # runs once per resampled key per group, and building a list would make an
        # O(log n) search allocate O(n) every time.
        i = bisect.bisect_right(buf, t_ns, key=lambda e: e.t_ns)
        lo = buf[i - 1] if i > 0 else None
        hi = buf[i] if i < len(buf) else None

        if lo is not None and lo.t_ns == t_ns:
            return Sampled(lo, "exact", 0)
        if lo is None:  # instant precedes this topic's first sample
            return Sampled(hi, "nearest", hi.t_ns - t_ns)
        if hi is None:  # instant is past the last sample: the live steady state
            return self._past_end(buf, lo, t_ns)
        return self._bracketed(lo, hi, t_ns, mode)

    def _bracketed(
        self, lo: Element, hi: Element, t_ns: Ns, mode: ResampleMode
    ) -> Sampled:
        residual = min(t_ns - lo.t_ns, hi.t_ns - t_ns)
        if mode == "hold_last":
            return Sampled(lo, "held", t_ns - lo.t_ns)
        if mode == "interpolate":
            mixed = blend(lo, hi, t_ns)
            if mixed is not None:
                return Sampled(mixed, "interpolated", residual)
            # No interpolator for this type (a Frame, a Record) — fall through to
            # nearest rather than refuse. `method` says which happened.
        near = lo if (t_ns - lo.t_ns) <= (hi.t_ns - t_ns) else hi
        return Sampled(near, "nearest", residual)

    def _past_end(self, buf: list[Element], lo: Element, t_ns: Ns) -> Sampled:
        """No sample after the instant — the steady state of a live stream.

        Extrapolation needs a slope, so it needs two samples and an interpolator;
        with either missing this holds instead. Holding is always available and
        always causal, which is why it is the default.
        """
        age = t_ns - lo.t_ns
        # A slope needs two samples at DIFFERENT times. Duplicate stamps are
        # routine on a merged live stream, and without this check `blend` would be
        # asked for a zero span.
        if (
            self._on_future == "extrapolate"
            and len(buf) >= 2
            and buf[-2].t_ns < lo.t_ns
        ):
            projected = blend(buf[-2], lo, t_ns)  # w > 1: past `hi`
            if projected is not None:
                return Sampled(projected, "extrapolated", age)
        return Sampled(lo, "held", age)
