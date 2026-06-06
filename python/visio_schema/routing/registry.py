"""ChannelRegistry — the single-source topic/schema table for one peer.

A stream is named globally by a topic string (e.g. ``/glove_left/imus/3/raw``)
and labelled on the wire by a numeric ``stream_id``. This registry maps
``stream_id -> Channel`` (topic + schema), for **one source**:

  * a schema-only consumer reads ONE link and feeds the registry — the device's
    announce and its data frames share one id space, so there is one source;
  * on a bus, the **bus** remaps every endpoint's local ids into its global id
    space *before* the registry sees them, so the registry is again single-source.

Because it is single-source there is no ``source_key`` and no per-source table:
just own outputs (declared locally) + learned channels (from announces, already
in this peer's id space) in one ``id -> Channel`` map, with the invariant that a
**topic maps to exactly one id** (a second id for a live topic is an error). The
bus calls :meth:`forget` when a link drops so the topic frees for a reconnect.

The whole no-bus consume path is::

    reg = ChannelRegistry()
    for msg, ch in reg.resolved(read_frames(serial)):
        writer.write(ch, msg)
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator

from visio_schema.routing.channel import Channel, DuplicateTopicError, Routed
from visio_schema.v1.service.device_info.device_info_pb2 import DeviceInfo
from visio_schema.wire.control import DEVICE_INFO as _DEVICE_INFO
from visio_schema.wire.control import FIRST_DYNAMIC
from visio_schema.wire.message import Message
from visio_schema.wire.schema import file_descriptor_set

log = logging.getLogger(__name__)

_PROTOBUF = "protobuf"

# Well-known channel for the DeviceInfo control stream, so a recorder can resolve
# and write forwarded announces on one "/device_info" topic (devices are told
# apart by the device_name field inside each message). It lives at the control id
# DEVICE_INFO and is resolution-only — never an own output, never announced.
DEVICE_INFO_TOPIC = "/device_info"
DEVICE_INFO_SCHEMA = "visio_schema.v1.service.device_info.DeviceInfo"


class ChannelRegistry:
    """Single-source ``stream_id -> Channel`` table + own outputs + discovery."""

    def __init__(
        self,
        device_name: str = "",
        firmware_version: str = "",
        hardware_revision: str = "",
        serial: str = "",
        boot_unix_seconds: int = 0,
        *,
        channels: Iterable = (),
    ) -> None:
        self._device_name = device_name
        self._firmware_version = firmware_version
        self._hardware_revision = hardware_revision
        self._serial = serial
        self._boot_unix_seconds = boot_unix_seconds

        self._by_id: dict[int, Channel] = {}        # id -> Channel (own + learned)
        self._topic_to_id: dict[str, int] = {}      # topic -> id (unique)
        self._own_ids: set[int] = set()             # which ids are our own outputs
        # One id allocator for the whole peer: own outputs (declare) AND the
        # learned-channel globals the bus assigns (via alloc) draw from it, so
        # own and learned ids can never collide. Never reused.
        self._next_id = FIRST_DYNAMIC
        self._dropped_unmapped = 0

        # Resolution-only well-known channel (see DEVICE_INFO_TOPIC). Kept out of
        # _by_id so it never appears in channels()/own outputs/announces.
        self._device_info_channel = Channel(
            id=_DEVICE_INFO, topic=DEVICE_INFO_TOPIC, encoding=_PROTOBUF,
            schema_name=DEVICE_INFO_SCHEMA,
            schema=file_descriptor_set(DEVICE_INFO_SCHEMA),
            schema_encoding=_PROTOBUF,
        )

        for entry in channels:
            self._declare_entry(entry)

    def alloc(self) -> int:
        """Allocate the next stream id (monotonic, never reused). Used by
        :meth:`declare` for own outputs and by the bus for learned-channel
        globals — one counter, so the two id spaces never overlap."""
        cid = self._next_id
        self._next_id += 1
        return cid

    # ── Own outputs ─────────────────────────────────────────────────────

    def declare(
        self,
        topic: str,
        schema_name: str,
        schema: bytes = b"",
        *,
        encoding: str = _PROTOBUF,
        schema_encoding: str = _PROTOBUF,
    ) -> int:
        """Declare an output stream; return its (stable) local id. Idempotent
        per topic."""
        existing = self._topic_to_id.get(topic)
        if existing is not None:
            return existing
        cid = self.alloc()
        self._by_id[cid] = Channel(
            id=cid, topic=topic, encoding=encoding,
            schema_name=schema_name, schema=schema, schema_encoding=schema_encoding,
        )
        self._topic_to_id[topic] = cid
        self._own_ids.add(cid)
        return cid

    def declare_output(self, topic: str, schema_name: str) -> int:
        """Declare an output, resolving its ``FileDescriptorSet`` from the local
        descriptor pool so the channel self-describes. Idempotent per topic."""
        return self.declare(topic, schema_name, file_descriptor_set(schema_name))

    def local_id_for(self, topic: str) -> int:
        """Local id for a declared output topic. Raises KeyError if undeclared."""
        return self._topic_to_id[topic]

    def own_channels(self) -> list[Channel]:
        return [self._by_id[cid] for cid in self._own_ids]

    # ── Learned channels (from announces, already in this peer's id space) ──

    def learn(self, channel: Channel) -> None:
        """Record an announced channel by its (already-unified) id. Raises
        :class:`DuplicateTopicError` if the topic is already mapped to a different
        id; idempotent when re-announced with the same id."""
        existing = self._topic_to_id.get(channel.topic)
        if existing is not None and existing != channel.id:
            raise DuplicateTopicError(
                f"topic {channel.topic!r} is mapped to id {existing}; "
                f"refusing to also map it to id {channel.id}"
            )
        ch = Channel()
        ch.CopyFrom(channel)
        self._by_id[channel.id] = ch
        self._topic_to_id[channel.topic] = channel.id

    def forget(self, ids: Iterable[int]) -> None:
        """Drop channels by id (the bus calls this for a dropped link's ids so
        their topics free up for a reconnect)."""
        for cid in ids:
            ch = self._by_id.pop(cid, None)
            if ch is not None and self._topic_to_id.get(ch.topic) == cid:
                self._topic_to_id.pop(ch.topic, None)
            self._own_ids.discard(cid)

    # ── Resolution ──────────────────────────────────────────────────────

    def resolve(self, stream_id: int) -> Channel | None:
        """Channel for a stream id (own or learned data, or the well-known
        DeviceInfo control stream), or None."""
        if stream_id == _DEVICE_INFO:
            return self._device_info_channel
        return self._by_id.get(stream_id)

    def channels(self) -> list[Channel]:
        """All data channels this peer knows — own outputs + learned."""
        return list(self._by_id.values())

    @property
    def has_own_outputs(self) -> bool:
        """True if this peer has declared outputs to announce (own-only)."""
        return bool(self._own_ids)

    @property
    def dropped_unmapped(self) -> int:
        """Inbound data frames :meth:`accept` dropped because their id was not
        learned yet (the no-bus single-source path; the bus has its own counter)."""
        return self._dropped_unmapped

    # ── Inbound (no-bus single-source consumer) ─────────────────────────

    def accept(self, msg: Message) -> Routed:
        """Single-source inbound decision: DEVICE_INFO → learn → ``Routed(None,None)``;
        control → ``Routed(msg,None)``; data → ``Routed(msg, channel)`` if the id
        is known, else drop (bump :attr:`dropped_unmapped`). Used by a consumer
        reading one link; on a bus the bus remaps before the registry."""
        sid = msg.stream_id
        if sid == _DEVICE_INFO:
            self.on_announce(msg.payload)
            return Routed(None, None)
        if sid < FIRST_DYNAMIC:
            return Routed(msg, None)
        ch = self._by_id.get(sid)
        if ch is None:
            self._dropped_unmapped += 1
            return Routed(None, None)
        return Routed(msg, ch)

    def resolved(self, messages: Iterable[Message]) -> Iterator[tuple[Message, Channel]]:
        """Run inbound messages through :meth:`accept` and yield the resolved
        ``(message, channel)`` data rows. The clean consumer loop."""
        for m in messages:
            message, channel = self.accept(m)
            if channel is not None:
                yield message, channel

    # ── Discovery ────────────────────────────────────────────────────────

    def self_info(self) -> DeviceInfo:
        """This peer's DeviceInfo announcement — **own outputs only**. Learned
        channels propagate by the bus forwarding each leaf's announce (with the
        ids remapped), not by recombining them here."""
        return DeviceInfo(
            device_name=self._device_name,
            channels=self.own_channels(),
            firmware_version=self._firmware_version,
            hardware_revision=self._hardware_revision,
            serial=self._serial,
            boot_unix_seconds=self._boot_unix_seconds,
        )

    def on_announce(self, payload: bytes) -> None:
        """Learn every channel in a serialized DeviceInfo announce (the ids are
        already this peer's — single source). Malformed payloads and duplicate
        topics are logged, not raised, on this convenience path."""
        di = DeviceInfo()
        try:
            di.ParseFromString(payload)
        except Exception:
            log.warning("dropping malformed DeviceInfo announce")
            return
        for ch in di.channels:
            try:
                self.learn(ch)
            except DuplicateTopicError as exc:
                log.error("announce: %s", exc)

    def _declare_entry(self, entry) -> int:
        if isinstance(entry, Channel):
            return self.declare(
                entry.topic, entry.schema_name, entry.schema,
                encoding=entry.encoding or _PROTOBUF,
                schema_encoding=entry.schema_encoding or _PROTOBUF,
            )
        topic, schema_name = entry
        return self.declare_output(topic, schema_name)
