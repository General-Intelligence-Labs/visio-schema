"""SerialEndpoint — COBS-delimited core-frames over a serial / pty fd.

A thin alias of :class:`FramedFdEndpoint`: an active object that owns its own I/O
thread. Construct with a fixed fd (a pty for tests/interop, or ``/dev/ttyGS0``),
or pass ``factory=`` for a reopenable link. The CDC-ACM liveness watchdog is
firmware/C++ only; the Python endpoint does not reconnect unless given a factory.
"""
from __future__ import annotations

from visio_schema.transport.framed_fd import FramedFdEndpoint


class SerialEndpoint(FramedFdEndpoint):
    """COBS-framed core-frame active-object endpoint over a serial / pty fd."""
