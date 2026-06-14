"""SerialEndpoint — COBS-delimited core-frames over a serial / pty fd.

A thin :class:`FramedFdEndpoint`: an active object that owns its own I/O thread.
Construct with either a device ``path`` (opened once) or a pre-opened ``fd`` (a
pty for tests/interop, or an already-open ``/dev/ttyGS0``) — exactly one of the
two.

The link is **fixed**: this host-side endpoint does not auto-reconnect. A flaky
device that re-enumerates can come back on a *different* host node (``/dev/ttyACMn``
is pool-allocated and races on re-enumeration), so silently reopening a cached
path would reattach to the wrong device. Reconnect is therefore the caller's job:
re-resolve identity (prefer ``/dev/serial/by-id/...`` or TCP to the device's mDNS
name) and construct a fresh endpoint. The CDC-ACM gadget liveness watchdog is
firmware/C++ only (it keys off device-side gadget state that does not exist here).
"""
from __future__ import annotations

from visio_schema.transport.endpoint import EndpointClosed
from visio_schema.transport.framed_fd import FramedFdEndpoint
from visio_schema.transport.link import open_serial_fd


class SerialEndpoint(FramedFdEndpoint):
    """COBS-framed core-frame active-object endpoint over a serial / pty fd."""

    def __init__(
        self,
        fd: int | None = None,
        *,
        path: str | None = None,
        max_outbox_frames: int | None = None,
    ) -> None:
        if (fd is None) == (path is None):
            raise ValueError("SerialEndpoint takes exactly one of fd or path")
        if path is not None:
            fd = open_serial_fd(path)        # opened once; fixed link, no reopen
            if fd < 0:
                raise EndpointClosed(f"SerialEndpoint: cannot open {path!r}")
        kw = {} if max_outbox_frames is None else {"max_outbox_frames": max_outbox_frames}
        super().__init__(fd, **kw)
