"""Channel + the routing value types.

``Channel`` is the generated ``visio_schema.v1.service.device_info.Channel`` proto
(topic + schema, mirroring the Foxglove channel); it is re-exported here so the
routing layer has one name for it. ``Routed`` is the per-message decision the
registry returns; ``DuplicateTopicError`` is the unique-topic invariant.
"""
from __future__ import annotations

from typing import NamedTuple

from visio_schema.v1.service.device_info.device_info_pb2 import Channel
from visio_schema.wire.message import Message
from visio_schema.wire.schema import file_descriptor_set

__all__ = ["Channel", "DuplicateTopicError", "Routed", "make_channel"]

# The only message/schema encoding visio-schema channels use. Shared with the
# registry's own channel construction.
_PROTOBUF = "protobuf"


def make_channel(topic: str, schema_name: str, *, stream_id: int) -> Channel:
    """Build a self-describing `Channel` (topic + schema) for writing.

    Fills the channel's `FileDescriptorSet` from `schema_name` and defaults both
    encodings to ``protobuf``, so the result is ready to hand to `McapWriter.write`.
    This is the kind of channel a device announces for each output stream; you only
    build one yourself when producing data (e.g. writing a recording from scratch).

    Args:
        topic: The topic name, e.g. ``"/imu/0/raw"``.
        schema_name: The payload's full protobuf type name, e.g.
            ``"visio_schema.v1.sensor.ImuRaw"`` — must be a generated type.
        stream_id: The stream's numeric id; a data stream, so ``>= FIRST_DYNAMIC``
            (16). Keyword-only — the caller numbers its own outputs.

    Returns:
        A `Channel` with `schema` populated and both encodings set to ``protobuf``.

    Example:
        ch = make_channel("/imu/0/raw", "visio_schema.v1.sensor.ImuRaw", stream_id=16)
        writer.write(Message(stream_id=ch.id, payload=imu.SerializeToString()), ch)
    """
    return Channel(
        id=stream_id,
        topic=topic,
        encoding=_PROTOBUF,
        schema_name=schema_name,
        schema=file_descriptor_set(schema_name),
        schema_encoding=_PROTOBUF,
    )


class DuplicateTopicError(Exception):
    """A topic was announced under a second stream id while the first is still
    live. The bus deregisters a dropped link's ids before a reconnect re-announce,
    so this signals a real protocol/wiring fault."""


class Routed(NamedTuple):
    """Outcome of :meth:`ChannelRegistry.accept`. ``message`` is what to forward
    (None = drop/absorb); ``channel`` is what to record against (None = skip)."""

    message: Message | None
    channel: Channel | None
