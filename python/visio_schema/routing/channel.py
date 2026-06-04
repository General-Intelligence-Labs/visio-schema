"""Channel + the routing value types.

``Channel`` is the generated ``visio_schema.service.device_info.v1.Channel`` proto
(topic + schema, mirroring the Foxglove channel); it is re-exported here so the
routing layer has one name for it. ``Routed`` is the per-message decision the
registry returns; ``DuplicateTopicError`` is the unique-topic invariant.
"""
from __future__ import annotations

from typing import NamedTuple

from visio_schema.service.device_info.v1.device_info_pb2 import Channel
from visio_schema.wire.message import Message

__all__ = ["Channel", "Routed", "DuplicateTopicError"]


class DuplicateTopicError(Exception):
    """A topic was announced under a second stream id while the first is still
    live. The bus deregisters a dropped link's ids before a reconnect re-announce,
    so this signals a real protocol/wiring fault."""


class Routed(NamedTuple):
    """Outcome of :meth:`ChannelRegistry.accept`. ``message`` is what to forward
    (None = drop/absorb); ``channel`` is what to record against (None = skip)."""

    message: Message | None
    channel: Channel | None
