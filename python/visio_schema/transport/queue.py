"""QueueEndpoint — in-process consumer sink. Attach as a sink to capture
messages for local consumption; thread-safe pop so a non-bus thread can drain
what the bus thread enqueued.
"""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable

from visio_schema.transport.endpoint import Endpoint
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

    def fileno(self) -> int | None:
        return None     # not fd-driven; never read by the bus selector

    def try_read(self) -> Iterable[Message]:
        return ()       # bus never reads us as a source

    def write(self, msg: Message) -> None:
        if self._filter is not None and msg.stream_id != self._filter:
            return
        with self._lock:
            self._queue.append(msg)

    def close(self) -> None:
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
