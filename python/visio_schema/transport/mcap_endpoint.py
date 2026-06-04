"""McapEndpoint — a sink Endpoint that records messages to MCAP.

A transport Endpoint (like SerialEndpoint), but its "wire" is an MCAP file: it
wraps :class:`visio_schema.mcap.McapWriter` and resolves each message's stream_id
to its :class:`Channel` via a ``resolve(stream_id) -> Channel`` callback
(typically ``bus.registry.resolve``, or a plain ``ChannelRegistry.resolve`` with
no bus). A message whose id does not resolve (no DeviceInfo announce seen) is
dropped (drop-until-mapped). No bus dependency.

``output`` and the ``compression`` / ``max_bytes`` / ``max_duration_s`` rotation
options pass straight through to :class:`McapWriter`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import IO

from visio_schema.mcap import McapWriter
from visio_schema.service.device_info.v1.device_info_pb2 import Channel
from visio_schema.transport.endpoint import Endpoint
from visio_schema.wire.message import Message

__all__ = ["McapEndpoint"]

# resolve(stream_id) -> Channel | None. Typically bus.registry.resolve.
StreamResolver = Callable[[int], "Channel | None"]


class McapEndpoint(Endpoint):
    """Sink Endpoint that records messages to MCAP via an :class:`McapWriter`.

    `output`: a filesystem path, or an already-open **seekable** binary stream.
    `resolve`: `resolve(stream_id) -> Channel | None` (topic + schema for an id).
    `compression` / `max_bytes` / `max_duration_s`: forwarded to McapWriter.
    """

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
        self._closed = False

    def fileno(self) -> int | None:
        return None     # not fd-driven; never read by the bus selector

    def try_read(self) -> Iterable[Message]:
        return ()       # sink-only

    def write(self, msg: Message) -> None:
        if self._closed:
            return
        ch = self._resolve(msg.stream_id)
        if ch is None:
            return      # drop-until-mapped
        self._writer.write(ch, msg)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.close()
