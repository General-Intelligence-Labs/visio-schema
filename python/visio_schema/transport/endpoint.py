"""Endpoint — an ACTIVE OBJECT: one self-contained, self-threaded connection.

Each endpoint owns its concurrency. ``start()`` spawns the endpoint's own I/O
thread; the endpoint does its own fd polling, outbound queueing, writes, reads,
and reconnect — none of it on the caller's thread. The bus is therefore a thin
router with no I/O threads of its own: it ``send()``s to sinks (a thread-safe,
non-blocking enqueue) and receives inbound via the ``on_inbound`` callback the
endpoint invokes from its OWN thread.

  start(on_inbound, on_closed): spawn the I/O thread. ``on_inbound(msg, ep)`` is
    called from that thread for each decoded inbound message; ``on_closed(ep)`` is
    called once if a FIXED link hits EOF (the owner then detaches it). Reopenable
    endpoints self-heal and never call on_closed. A write-only sink (the recorder)
    ignores both callbacks.
  send(msg): thread-safe, non-blocking — enqueue for sending; the endpoint's own
    thread performs the actual write. Sheds on a full/stalled link; never blocks.
  stop(): stop + join the I/O thread, close the link. Idempotent.

Lives in visio-schema so a schema-only user can run one stream with no bus.
Mirrors the C++ ``visio_schema::transport::Endpoint`` ABC.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from visio_schema.wire.message import Message

# on_inbound(msg, ep): called from the endpoint's OWN thread per decoded message.
InboundFn = Callable[[Message, "Endpoint"], None]
# on_closed(ep): called from the endpoint's OWN thread once a fixed link hits EOF.
ClosedFn = Callable[["Endpoint"], None]


class EndpointClosed(Exception):
    """Raised by the byte/link layer when a connection breaks (EOF, broken pipe).

    Surfaced to the endpoint's own thread, which either self-heals (reopenable) or
    reports it via ``on_closed`` (fixed link)."""


class Endpoint(ABC):
    """Abstract active-object endpoint. Concrete subclasses implement all three."""

    @abstractmethod
    def start(self, on_inbound: InboundFn | None, on_closed: ClosedFn | None) -> None:
        """Spawn the endpoint's I/O thread. Either callback may be None."""

    @abstractmethod
    def send(self, msg: Message) -> None:
        """Thread-safe, non-blocking enqueue for sending (drains on the endpoint's
        own thread)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop + join the I/O thread and close the link. Idempotent."""
