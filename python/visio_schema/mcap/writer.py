"""McapWriter — the canonical Visio MCAP writer.

Writes a spec-conformant, Foxglove-readable MCAP from ``(message, channel)``
pairs: a protobuf channel's ``Schema.name`` is the payload's protobuf full name
and ``Schema.data`` is its ``FileDescriptorSet`` (both carried on the `Channel`),
so Foxglove resolves the type from the embedded set.

``mcap`` is a default dependency, imported lazily; the writer raises a clear error
if it is missing from the environment.
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from visio_schema.v1.service.device_info.device_info_pb2 import Channel
from visio_schema.wire.message import Message

__all__ = ["McapWriter"]

_INSTALL_HINT = (
    "MCAP support needs the 'mcap' package — install it with "
    "`pip install mcap`."
)


def _writer_api():
    try:
        from mcap.writer import CompressionType, Writer
    except ImportError as exc:  # pragma: no cover
        raise ImportError(_INSTALL_HINT) from exc
    return Writer, CompressionType


def _is_seekable(stream: IO[bytes]) -> bool:
    try:
        return bool(stream.seekable())
    except (AttributeError, OSError):
        return False


class McapWriter:
    """Write ``(message, channel)`` pairs to an MCAP file (Foxglove's container format).

    Payload bytes are stored verbatim (already-serialized protobuf); topic + schema
    come from the `Channel` you pass. Schema and channel records are registered lazily
    (one per `Channel.schema_name` / `Channel.id`), and the caller decides what to
    write (unlike `McapWriterEndpoint`, which resolves and drops-until-mapped). Usable
    as a context manager — `close` finalizes the file(s). Needs the ``mcap``
    dependency (installed by default; ``pip install mcap`` if missing).

    Args:
        output: A filesystem path, or an already-open **seekable** binary stream (a
            regular file or ``io.BytesIO``; a caller-supplied stream is left open on
            `close`). The mcap writer calls ``.tell()``, so a pipe/FIFO/socket is
            rejected.
        compression: An ``mcap.writer.CompressionType`` (default: none); keyword-only.
        max_bytes: If set, rotate into numbered parts ``name_0000.mcap``,
            ``name_0001.mcap``, … once a part exceeds this many written payload bytes
            (approximate — a part overshoots by at most one message). Path output
            only; keyword-only.
        max_duration_s: Like `max_bytes`, but rotate by elapsed log time. Path output
            only; keyword-only.

    Example:
        with McapWriter("run.mcap") as w:
            for msg, channel in read_serial("/dev/ttyACM0"):
                w.write(msg, channel)
    """

    def __init__(
        self,
        output: str | Path | IO[bytes],
        *,
        compression=None,
        max_bytes: int | None = None,
        max_duration_s: float | None = None,
        profile: str = "",
        library: str | None = None,
    ) -> None:
        self._Writer, CompressionType = _writer_api()
        # Written into every part's header. `profile` names the ecosystem a reader
        # should interpret the file under; `library` identifies the producer, which
        # is the first thing you want when two tools wrote files that differ.
        #
        # Both default to what the mcap writer already produced, so adding them
        # changes no existing output — a caller that wants `profile="visio"` says
        # so. Making "visio" the default would silently restamp the header of every
        # recording this writes, which is a wire-visible change and belongs in its
        # own decision, not in a refactor.
        self._profile = profile
        self._library = library
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
        # Replayed into every rolled part (see `_open_part`), so each part stands
        # alone. Must exist before the first `_open_part()` below.
        self._metadata: list[tuple[str, dict[str, str]]] = []

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

    def write(self, msg: Message, channel: Channel) -> None:
        """Record one message on a channel.

        Schema and channel records are registered lazily on first use. Argument order
        is ``(message, channel)`` — matching what `read_mcap`, `read_serial`, and
        `ChannelRegistry.resolved` yield — so a read round-trips to a write without
        reordering.

        Channels are deduplicated by **topic**, not by `channel.id`, so merging
        several sources into one file is safe.

        Args:
            msg: The `Message` to record; its `payload` is stored verbatim and its
                `timestamp` becomes the MCAP log time.
            channel: The `Channel` (topic + schema) to record it on — from a read row
                or built with `make_channel`.

        Raises:
            ValueError: The topic was already written with a different schema.

        Example:
            # copy, or merge several sources into one file
            for src in ("rec.mcap", "depth.mcap", "vio.mcap"):
                for msg, channel in read_mcap(src):
                    writer.write(msg, channel)
        """
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

        # Keyed by TOPIC, never by `channel.id`: that id is only unique within one
        # file (`read_mcap` fills it from the MCAP channel) or one bus (`make_channel`
        # fills it with a stream id), so keying on it aliases channels across merged
        # sources.
        channel_id = self._channel_ids.get(channel.topic)
        if channel_id is None:
            channel_id = self._writer.register_channel(
                topic=channel.topic,
                message_encoding=channel.encoding or "protobuf",
                schema_id=schema_id,
            )
            self._channel_ids[channel.topic] = channel_id
            self._channel_schemas[channel.topic] = channel.schema_name
        elif self._channel_schemas[channel.topic] != channel.schema_name:
            # One topic carrying two schemas is an unreadable file, not a merge —
            # surface it instead of silently binding to whichever arrived first.
            raise ValueError(
                f"topic {channel.topic!r} already registered with schema "
                f"{self._channel_schemas[channel.topic]!r}; refusing to also write "
                f"{channel.schema_name!r} to it"
            )

        ts = msg.timestamp.ToNanoseconds()
        self._writer.add_message(
            channel_id=channel_id,
            log_time=ts,
            publish_time=ts,
            sequence=msg.seq,
            data=msg.payload,
        )
        self._part_bytes += len(msg.payload)

    def add_metadata(self, name: str, kv: Mapping[str, str]) -> None:
        """Attach a named metadata record — where provenance belongs.

        An MCAP metadata record is a string→string map written outside any
        channel, so a consumer reads it without decoding messages. That is the
        right home for provenance (``visio.capture`` from a recorder,
        ``visio.derived`` from a post-processing stage) rather than a synthetic
        channel that every reader then has to know to skip.

        Re-emitted into **every** rolled part, matching `_open_part`'s
        stands-alone rule: a session's part 3 must still say where it came from.
        Call it before or between writes; ordering within a part is not
        significant.

        Args:
            name: The record name, e.g. ``"visio.derived"``.
            kv: String→string map. MCAP metadata is strictly textual, so a
                non-string value is rejected here rather than deep inside the
                mcap writer.

        Raises:
            TypeError: A value is not a ``str`` (the offending key is named).
            RuntimeError: The writer is already closed.
        """
        if self._closed:
            raise RuntimeError(
                f"McapWriter is closed; metadata {name!r} would be dropped. "
                "An MCAP with no provenance record is indistinguishable from one "
                "where the call never happened, so this fails rather than "
                "silently discarding it."
            )
        for key, value in kv.items():
            if not isinstance(value, str):
                raise TypeError(
                    f"metadata {name!r} key {key!r} has "
                    f"{type(value).__name__} value {value!r}; MCAP metadata is "
                    "string->string — format it before passing it in"
                )
        record = (name, dict(kv))
        self._metadata.append(record)
        self._writer.add_metadata(record[0], record[1])

    def close(self) -> None:
        """Finalize and close the file(s). Idempotent. Prefer the context-manager form
        (``with McapWriter(...) as w:``), which closes automatically."""
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
        # 4-digit zero-pad (matches the C++ writer's NumberedPart): parts stay
        # lexicographically ordered through 9999. At 3 digits, part 1000 sorts
        # before part 999, breaking the chronological order the uploader and
        # playback rely on once a session exceeds 999 parts.
        return self._path.with_name(
            f"{self._path.stem}_{self._part_index:04d}{self._path.suffix}"
        )

    def _open_part(self) -> None:
        # Each part re-registers its own schemas/channels so it stands alone.
        self._schema_ids: dict[str, int] = {}
        self._channel_ids: dict[str, int] = {}  # topic -> this part's channel id
        self._channel_schemas: dict[str, str] = {}  # topic -> schema, to catch conflicts
        self._part_start_ns = time.monotonic_ns()
        self._part_bytes = 0
        if self._path is not None:
            self._file = open(self._part_path(), "wb")
        self._writer = self._Writer(self._file, compression=self._compression)
        start_kw = {}
        if self._profile:
            start_kw["profile"] = self._profile
        if self._library is not None:
            start_kw["library"] = self._library
        self._writer.start(**start_kw)
        for name, kv in self._metadata:
            self._writer.add_metadata(name, kv)

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
