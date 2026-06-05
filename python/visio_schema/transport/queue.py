"""QueueEndpoint — in-process consumer sink (active object, but synchronous: no
thread). ``send()`` appends under a lock so a non-bus thread can drain via
``pop_all()``. Mirrors the C++ ``QueueEndpoint``.
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
    are queued.
    """

    def __init__(self, stream_filter: int | None = None) -> None:
        self._filter = stream_filter
        self._queue: deque[Message] = deque()
        self._lock = threading.Lock()

    def start(self, on_inbound: InboundFn | None = None,
              on_closed: ClosedFn | None = None) -> None:
        pass  # synchronous: no thread

    def send(self, msg: Message) -> None:
        if self._filter is not None and msg.stream_id != self._filter:
            return
        with self._lock:
            self._queue.append(msg)

    def stop(self) -> None:
        pass

    def pop_all(self) -> list[Message]:
        """Atomically drain and return all queued messages."""
        with self._lock:
            msgs = list(self._queue)
            self._queue.clear()
        return msgs

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)
