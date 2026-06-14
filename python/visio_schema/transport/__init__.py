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
    """Open a bidirectional serial `Endpoint`, preferring the native reader.

    Returns a native GIL-free endpoint when the compiled ``_creader`` extension is
    available (and not disabled via ``VISIO_NO_NATIVE=1``), otherwise a pure-Python
    one; both implement the same `Endpoint` interface, so callers don't care which.
    Use this when you need to send to the device (e.g. commands) as well as read; for
    read-only viewing, `read_serial` is simpler.

    Args:
        path: Serial device path, e.g. ``"/dev/ttyACM0"``.
        max_depth: Max inbound messages buffered before the reader sheds, bounding
            back-pressure; keyword-only.

    Returns:
        An `Endpoint`. Call ``start(on_inbound, on_closed)`` to begin reading and
        ``send(msg)`` to transmit; ``stop()`` to shut down.

    Example:
        ep = serial_endpoint("/dev/ttyACM0")
        ep.start(lambda msg, _ep: print(msg.stream_id), None)
        ep.send(command_message(cmd))
        ep.stop()
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
