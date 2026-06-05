"""McapEndpoint — a write-only sink ACTIVE OBJECT that records messages to MCAP.

``send()`` (called on the bus dispatch thread) resolves + snapshots the channel
and enqueues; the endpoint's OWN writer thread drains it to disk, so the blocking
file write never touches the bus dispatch lock. Resolution happens on the send()
caller (dispatch, serialized), so the writer thread never touches the registry.
Ignores on_inbound (write-only). Mirrors the C++ ``McapEndpoint``.

A message whose id does not resolve (no DeviceInfo announce seen) is dropped
(drop-until-mapped). ``output`` and the rotation options pass through to
:class:`McapWriter`.
"""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import IO

from visio_schema.mcap import McapWriter
from visio_schema.service.device_info.v1.device_info_pb2 import Channel
from visio_schema.transport.endpoint import ClosedFn, Endpoint, InboundFn
from visio_schema.wire.message import Message

__all__ = ["McapEndpoint"]

# resolve(stream_id) -> Channel | None. Typically bus.registry.resolve.
StreamResolver = Callable[[int], "Channel | None"]


class McapEndpoint(Endpoint):
    """Write-only sink Endpoint that records to MCAP on its own writer thread."""

    def __init__(
        self,
        output: str | Path | IO[bytes],
        resolve: StreamResolver,
        *,
        compression=None,
        max_bytes: int | None = None,
        max_duration_s: float | None = None,
    ) -> None:
        self._resolve = resolve
        self._writer = McapWriter(
            output,
            compression=compression,
            max_bytes=max_bytes,
            max_duration_s=max_duration_s,
        )
        self._queue: deque[tuple[Channel, Message]] = deque()
        self._cv = threading.Condition()
        self._stop = False
        self._thread: threading.Thread | None = None

    def start(self, on_inbound: InboundFn | None = None,
              on_closed: ClosedFn | None = None) -> None:
        with self._cv:
            self._stop = False
        if self._thread is None:
            self._thread = threading.Thread(target=self._writer_loop, daemon=True)
            self._thread.start()

    def send(self, msg: Message) -> None:
        # Resolve on the caller (dispatch) thread; the writer thread is then
        # independent of the (non-thread-safe) registry.
        ch = self._resolve(msg.stream_id)
        if ch is None:
            return  # drop-until-mapped
        with self._cv:
            self._queue.append((ch, msg))
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            if self._stop:
                return
            self._stop = True
            self._cv.notify()
        if self._thread is not None:
            self._thread.join()           # drains the remaining queue
            self._thread = None
        self._writer.close()

    def _writer_loop(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait()
                batch = list(self._queue)
                self._queue.clear()
                if not batch and self._stop:
                    return                # stopped + fully drained
            for ch, msg in batch:
                self._writer.write(ch, msg)
