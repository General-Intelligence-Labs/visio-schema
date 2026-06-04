"""read_mcap — replay an MCAP recording as ``(Message, Channel)`` pairs.

The same shape :meth:`ChannelRegistry.resolved` yields from a live stream, so a
recording and a live link feed a sink identically. Each MCAP channel is
self-describing (topic + schema records), so the :class:`Channel` is rebuilt
straight from the file with no DeviceInfo. ``mcap`` is an optional dependency
(``pip install visio-schema[mcap]``), imported lazily.
"""
from __future__ import annotations

import logging
from pathlib import Path

from google.protobuf.timestamp_pb2 import Timestamp

from visio_schema.service.device_info.v1.device_info_pb2 import Channel
from visio_schema.wire.message import Message

__all__ = ["read_mcap"]

log = logging.getLogger(__name__)

_INSTALL_HINT = (
    "MCAP support needs the 'mcap' package — install it with "
    "`pip install visio-schema[mcap]`."
)


def _reader_api():
    try:
        from mcap.reader import make_reader
    except ImportError as exc:  # pragma: no cover
        raise ImportError(_INSTALL_HINT) from exc
    return make_reader


def read_mcap(path: str | Path):
    """Replay an MCAP recording as ``(Message, Channel)`` pairs. Channels with no
    schema record are skipped (their payload type is unresolvable)."""
    make_reader = _reader_api()
    with open(path, "rb") as f:
        for schema, channel, message in make_reader(f).iter_messages():
            if schema is None:
                log.warning("skip: MCAP channel %r has no schema", channel.topic)
                continue
            ch = Channel(
                id=channel.id,
                topic=channel.topic,
                encoding=channel.message_encoding or "protobuf",
                schema_name=schema.name,
                schema=schema.data,
                schema_encoding=schema.encoding or "protobuf",
            )
            ts = Timestamp()
            ts.FromNanoseconds(message.log_time)
            msg = Message(
                stream_id=ch.id,
                payload=message.data,
                seq=message.sequence,
                timestamp=ts,
            )
            yield msg, ch
