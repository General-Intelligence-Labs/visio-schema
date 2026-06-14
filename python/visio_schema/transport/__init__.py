"""visio_schema.transport — active-object endpoints that move Messages over a fd.

A schema-only user reads from / writes to ONE visio stream with these and no bus:
get an fd (``open_serial_fd`` / ``make_fd_pair``), wrap it in a
:class:`SerialEndpoint`, ``start(on_inbound, on_closed)`` it, and ``send()``.
Every Endpoint is an active object that owns its own I/O thread; the byte layer is
plain fd helpers (no Link object — the fd IS the link). A fixed fd reports EOF via
``on_closed``; a reopenable one (``factory=``) self-heals. The MCAP sink/source
endpoints (:class:`~visio_schema.mcap.McapWriterEndpoint` /
:class:`~visio_schema.mcap.McapReaderEndpoint`) live in :mod:`visio_schema.mcap`.
"""
from visio_schema.transport.endpoint import (
    ClosedFn,
    Endpoint,
    EndpointClosed,
    InboundFn,
)
from visio_schema.transport.framed_fd import FramedFdEndpoint
from visio_schema.transport.framing import extract_frames, frame_bytes, read_frames
from visio_schema.transport.link import (
    FdFactory,
    close_fd,
    make_fd_pair,
    open_serial_fd,
    read_some,
    set_nonblocking,
    set_raw_mode,
    write_some,
)
from visio_schema.transport.native_serial import HAVE_NATIVE, NativeSerialEndpoint
from visio_schema.transport.queue import QueueEndpoint
from visio_schema.transport.serial import SerialEndpoint


def serial_endpoint(path: str, *, max_depth: int = 4096) -> Endpoint:
    """Open a serial Endpoint, preferring the native GIL-free reader.

    Returns a :class:`NativeSerialEndpoint` when the native ``_creader`` extension
    is importable and not disabled via ``VISIO_NO_NATIVE=1``; otherwise the
    pure-Python :class:`SerialEndpoint`. Both satisfy the ``Endpoint`` ABC, so the
    Bus and callers are agnostic to which one they get. Prefer this over
    constructing an endpoint class directly when you want the native fast path
    with an automatic fallback.
    """
    import os

    if os.environ.get("VISIO_NO_NATIVE") != "1" and HAVE_NATIVE:
        return NativeSerialEndpoint(path, max_depth=max_depth)
    return SerialEndpoint(path=path)


__all__ = [
    "HAVE_NATIVE",
    "ClosedFn",
    "Endpoint",
    "EndpointClosed",
    "FdFactory",
    "FramedFdEndpoint",
    "InboundFn",
    "NativeSerialEndpoint",
    "QueueEndpoint",
    "SerialEndpoint",
    "close_fd",
    "extract_frames",
    "frame_bytes",
    "make_fd_pair",
    "open_serial_fd",
    "read_frames",
    "read_some",
    "serial_endpoint",
    "set_nonblocking",
    "set_raw_mode",
    "write_some",
]
