"""Layer 1 — streaming alignment ops over the one ``Element`` iterator (§8.3).

There is **no public ``merge``**: the timestamp-ordered union of a session's
files is the Session's own job (§8.2), so ``Session.stream(...)`` is already the
single merged stream. These ops only *reshape* that one stream — ``sync`` groups
keys into time-matched sets, ``resample`` attaches low-rate side data — and are
bounded (state ≤ ``window`` / one sample per attach topic).

There is no sparse-sampling op either. Thinning a stream *after* decode is the
one reshaping that cannot pay for itself: decode is the cost, so a filter placed
above ``Session`` has already spent everything it saves. That job belongs to the
source, and it lives there — ``Session.keyframe_stream``.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from collections.abc import Iterable, Iterator
from typing import Literal, TypeVar

from .domain import Element, Ns, SyncGroup

_T = TypeVar("_T")


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


def sync(
    stream: Iterable[Element],
    keys: Iterable[str],
    *,
    tol_ns: Ns,
    window: int = 8,
    passthrough: bool = False,
    on_incomplete: Literal["drop", "emit_partial"] = "drop",
) -> Iterator[SyncGroup | Element]:
    """N-way nearest-timestamp sync over the single interleaved ``stream``.

    Buffers ≤ ``window`` elements per key; emits a ``SyncGroup`` when every key
    has one within ``tol_ns``, then advances; the oldest unmatched head is
    dropped (its partner was lost). Stereo = ``keys=(cam0, cam1)``; N-cam = N
    keys. ``passthrough=True`` also yields non-key elements (e.g. ``ImuSample``)
    inline in arrival order — VIO's single feed. ``on_incomplete="emit_partial"``
    yields dropped singletons as bare elements instead of discarding them.

    A key may name ANY streamed topic, not only a camera: a derived sidecar topic
    (`Record`) joins its frames here, through the same grouping that pairs the
    stereo cameras, rather than through a second lookup mechanism. That is why the
    member type is `Element` — the group is "one thing per key at this instant".
    """
    keys = list(keys)
    keyset = set(keys)
    emit_partial = on_incomplete == "emit_partial"
    buffers: dict[str, deque[Element]] = {k: deque() for k in keys}

    for el in stream:
        if el.topic in keyset:
            buf = buffers[el.topic]
            buf.append(el)
            if len(buf) > window:
                old = buf.popleft()
                if emit_partial:
                    yield old
            yield from _drain_groups(buffers, keys, tol_ns, emit_partial)
        elif passthrough:
            yield el
    yield from _drain_groups(buffers, keys, tol_ns, emit_partial)


def _drain_groups(
    buffers: dict[str, deque[Element]],
    keys: list[str],
    tol_ns: Ns,
    emit_partial: bool,
) -> Iterator[SyncGroup | Element]:
    """Emit every complete group formable from the buffer heads (§8.3)."""
    while all(buffers[k] for k in keys):
        heads = {k: buffers[k][0] for k in keys}
        ts = [e.t_ns for e in heads.values()]
        lo, hi = min(ts), max(ts)
        if hi - lo <= tol_ns:
            for k in keys:
                buffers[k].popleft()
            yield SyncGroup(t_ns=lo, by_topic=heads, residual_ns=hi - lo)
        else:
            oldest = min(keys, key=lambda k: buffers[k][0].t_ns)
            dropped = buffers[oldest].popleft()
            if emit_partial:
                yield dropped


def resample(
    stream: Iterable[Element],
    onto: str,
    attach: Iterable[str],
    *,
    mode: Literal["hold_last", "nearest"] = "hold_last",
) -> Iterator[tuple[Element, dict[str, Element]]]:
    """For each element on ``onto``, attach recent samples from ``attach`` topics.

    ``hold_last`` (streaming, 1-sample state per attach topic) attaches the most
    recent sample seen on each attach topic — the right choice for low-rate side
    data (e.g. a VIO pose held onto each frame). ``nearest`` is not yet
    implemented (needs a bounded lookahead; see alignment §9).
    """
    if mode == "nearest":
        raise NotImplementedError(
            "resample(mode='nearest') is not implemented yet; use 'hold_last'"
        )
    attach = list(attach)
    last: dict[str, Element | None] = {a: None for a in attach}
    for el in stream:
        if el.topic in last:
            last[el.topic] = el
        elif el.topic == onto:
            yield el, {a: last[a] for a in attach if last[a] is not None}
