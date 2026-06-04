"""Endpoint — sync read+write interface to one connection.

An `Endpoint` is one ABC with both a read side and a write side. The bus decides
whether to drive reads, writes, or both by which slot it attaches to:

- `bus.attach_source(ep)` — bus reads via `try_read()`.
- `bus.attach_sink(ep)`   — bus writes via `write()`. Also reads control
  messages via `try_read()` (timesync replies, command acks, etc.).

Endpoints do NOT own threads, and they do NOT reconnect: on a broken link they
raise :class:`EndpointClosed`, and the caller (the bus, or an app loop) decides
what to do (detach, dial a fresh link, …). For local in-process consumption use
`QueueEndpoint`. Lives in visio-schema so a schema-only user can read/write one
stream with no bus.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from visio_schema.wire.message import Message


class EndpointClosed(Exception):
    """Raised by an Endpoint/Link when its connection breaks (EOF, broken pipe).

    Endpoints never reconnect themselves — they raise this so the caller decides
    what to do (the bus detaches + deregisters the endpoint; an app may dial a
    fresh link and re-attach)."""


class Endpoint(ABC):
    """Abstract endpoint. Concrete subclasses implement all four methods.

    Sink-only endpoints (e.g. MCAP file) leave `fileno()` returning None and
    `try_read()` yielding nothing. `try_read()`/`write()` raise
    :class:`EndpointClosed` when the underlying link breaks.
    """

    @abstractmethod
    def fileno(self) -> int | None:
        """Return the fd the bus should monitor for readable events, or None
        if this endpoint isn't fd-driven."""

    @abstractmethod
    def try_read(self) -> Iterable[Message]:
        """Called when fileno() is readable (or unconditionally when None).
        Returns zero or more decoded Messages. Raises :class:`EndpointClosed`
        on EOF / a broken link."""

    @abstractmethod
    def write(self, msg: Message) -> None:
        """Send `msg` to the peer. Raises :class:`EndpointClosed` on a broken
        link."""

    @abstractmethod
    def close(self) -> None:
        """Idempotent shutdown."""

