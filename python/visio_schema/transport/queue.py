"""QueueEndpoint — in-process consumer sink (active object, but synchronous: no
thread). ``send()`` appends under a lock; a non-bus consumer drains either
everything queued now (``pop_all()``) or blocks for a batch (``pop_batch()``).
Mirrors the C++ ``QueueEndpoint``.
"""
from __future__ import annotations

import threading
from collections import deque

from visio_schema.transport.endpoint import ClosedFn, Endpoint, InboundFn
from visio_schema.wire.message import Message

__all__ = ["QueueEndpoint"]


class QueueEndpoint(Endpoint):
    """In-process consumer Endpoint.

    `stream_filter`: if non-None, only messages with `msg.stream_id == filter`
    are queued. `max_depth`: None = unbounded (the historical default the test
    suite relies on); an int bounds the queue, shedding the oldest message
    (drop-oldest) when full — the pull-sink behavior.
    """

    def __init__(self, stream_filter: int | None = None,
                 max_depth: int | None = None) -> None:
        self._filter = stream_filter
        self._max_depth = max_depth
        self._queue: deque[Message] = deque()
        self._cv = threading.Condition()
        self._dropped = 0

    def start(self, on_inbound: InboundFn | None = None,
              on_closed: ClosedFn | None = None) -> None:
        pass  # synchronous: no thread

    def send(self, msg: Message) -> None:
        if self._filter is not None and msg.stream_id != self._filter:
            return
        with self._cv:
            if self._max_depth is not None:
                while len(self._queue) >= self._max_depth:
                    self._queue.popleft()          # drop-oldest, keep freshest
                    self._dropped += 1
            self._queue.append(msg)
            self._cv.notify()

    def stop(self) -> None:
        pass

    def pop_all(self) -> list[Message]:
        """Atomically drain and return all queued messages (non-blocking)."""
        with self._cv:
            msgs = list(self._queue)
            self._queue.clear()
        return msgs

    def pop_batch(self, timeout_ms: int = 200) -> list[Message]:
        """Wait up to ``timeout_ms`` for at least one message, then drain the batch."""
        with self._cv:
            if not self._queue:
                self._cv.wait(timeout_ms / 1000.0)
            msgs = list(self._queue)
            self._queue.clear()
        return msgs

    @property
    def dropped(self) -> int:
        return self._dropped

    def __len__(self) -> int:
        with self._cv:
            return len(self._queue)
