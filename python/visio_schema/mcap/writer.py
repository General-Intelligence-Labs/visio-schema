"""McapWriter — the canonical Visio MCAP writer.

Writes a spec-conformant, Foxglove-readable MCAP from ``(channel, message)``
pairs: a protobuf channel's ``Schema.name`` is the payload's protobuf full name
and ``Schema.data`` is its ``FileDescriptorSet`` (both carried on the
:class:`Channel`), so Foxglove resolves the type from the embedded set.

``mcap`` is an optional dependency: ``pip install visio-schema[mcap]``; the writer
imports it lazily and raises a clear error if it is missing.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import IO

from visio_schema.v1.service.device_info.device_info_pb2 import Channel
from visio_schema.wire.message import Message

__all__ = ["McapWriter"]

_INSTALL_HINT = (
    "MCAP support needs the 'mcap' package — install it with "
    "`pip install visio-schema[mcap]`."
)


def _writer_api():
    try:
        from mcap.writer import CompressionType, Writer
    except ImportError as exc:  # pragma: no cover - exercised via skip in tests
        raise ImportError(_INSTALL_HINT) from exc
    return Writer, CompressionType


def _is_seekable(stream: IO[bytes]) -> bool:
    try:
        return bool(stream.seekable())
    except (AttributeError, OSError):
        return False


class McapWriter:
    """Write ``(channel, message)`` pairs to MCAP, optionally rotating into parts.

    The payload bytes are stored verbatim (already-serialized protobuf); topic +
    schema come from the :class:`Channel` the caller passes. Schema/channel
    registration is lazy — one schema per ``Channel.schema_name``, one channel per
    ``Channel.id``. Unlike the transport endpoint this does no resolution or
    drop-until-mapped: the caller decides what to write (see
    :class:`~visio_schema.transport.mcap_endpoint.McapEndpoint` and
    :meth:`ChannelRegistry.resolved`).

    ``output``: a filesystem path, or an already-open **seekable** binary stream
    (a regular file or ``io.BytesIO``; a caller-supplied stream is left open on
    :meth:`close`). The sink must be seekable — the ``mcap`` writer calls
    ``.tell()`` — so a pipe/FIFO/socket is rejected at construction.

    ``compression``: an ``mcap.writer.CompressionType`` (default NONE).
    ``max_bytes`` / ``max_duration_s``: rotate into numbered parts
    ``name_000.mcap``, ``name_001.mcap``, … (path output only). Each part is a
    complete, self-contained MCAP. ``max_bytes`` counts written payload bytes, so
    treat it as approximate (a part overshoots by at most one message).

    Usable as a context manager; :meth:`close` finalizes the file(s).
    """

    def __init__(
        self,
        output: str | Path | IO[bytes],
        *,
        compression=None,
        max_bytes: int | None = None,
        max_duration_s: float | None = None,
    ) -> None:
        self._Writer, CompressionType = _writer_api()
        self._compression = (
            compression if compression is not None else CompressionType.NONE
        )
        self._max_bytes = max_bytes
        self._max_duration_ns = (
            int(max_duration_s * 1e9) if max_duration_s is not None else None
        )
        self._rotating = max_bytes is not None or max_duration_s is not None
        self._closed = False
        self._part_index = 0

        if isinstance(output, (str, Path)):
            self._path: Path | None = Path(output)
            self._owns_file = True
            self._file: IO[bytes] | None = None  # opened per part
        else:
            if self._rotating:
                raise ValueError(
                    "McapWriter rotation (max_bytes/max_duration_s) needs a path "
                    "output to name parts; got an open stream."
                )
            if not _is_seekable(output):
                raise ValueError(
                    "McapWriter needs a seekable sink (the mcap writer calls "
                    ".tell() and records byte offsets); a pipe/FIFO/socket is not "
                    "supported. Record to a file, or use io.BytesIO."
                )
            self._path = None
            self._file = output
            self._owns_file = False

        self._open_part()

    def __enter__(self) -> McapWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def write(self, channel: Channel, msg: Message) -> None:
        """Record one message against ``channel``. Lazily registers the channel's
        schema and channel records (declare-before-write)."""
        if self._closed:
            return

        if self._rotating and self._should_roll():
            self._roll()

        schema_id = self._schema_ids.get(channel.schema_name)
        if schema_id is None:
            schema_id = self._writer.register_schema(
                name=channel.schema_name,
                encoding=channel.schema_encoding or "protobuf",
                data=channel.schema,
            )
            self._schema_ids[channel.schema_name] = schema_id

        channel_id = self._channel_ids.get(channel.id)
        if channel_id is None:
            channel_id = self._writer.register_channel(
                topic=channel.topic,
                message_encoding=channel.encoding or "protobuf",
                schema_id=schema_id,
            )
            self._channel_ids[channel.id] = channel_id

        ts = msg.timestamp.ToNanoseconds()
        self._writer.add_message(
            channel_id=channel_id,
            log_time=ts,
            publish_time=ts,
            sequence=msg.seq,
            data=msg.payload,
        )
        self._part_bytes += len(msg.payload)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.finish()
        if self._owns_file and self._file is not None:
            self._file.close()

    # ── Internals ──────────────────────────────────────────────────────
    def _part_path(self) -> Path:
        assert self._path is not None
        if not self._rotating:
            return self._path
        return self._path.with_name(
            f"{self._path.stem}_{self._part_index:03d}{self._path.suffix}"
        )

    def _open_part(self) -> None:
        # Each part re-registers its own schemas/channels so it stands alone.
        self._schema_ids: dict[str, int] = {}
        self._channel_ids: dict[int, int] = {}
        self._part_start_ns = time.monotonic_ns()
        self._part_bytes = 0
        if self._path is not None:
            self._file = open(self._part_path(), "wb")
        self._writer = self._Writer(self._file, compression=self._compression)
        self._writer.start()

    def _should_roll(self) -> bool:
        # Don't roll an empty part: the size/age check must follow at least one
        # message, else a stale duration could spin out zero-message parts.
        if self._part_bytes == 0:
            return False
        if self._max_bytes is not None and self._part_bytes >= self._max_bytes:
            return True
        if self._max_duration_ns is not None:
            if time.monotonic_ns() - self._part_start_ns >= self._max_duration_ns:
                return True
        return False

    def _roll(self) -> None:
        self._writer.finish()
        if self._file is not None:
            self._file.close()
        self._part_index += 1
        self._open_part()
