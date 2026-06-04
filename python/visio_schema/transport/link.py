"""Link — the raw byte interface beneath Endpoints.

A Link is a byte channel that exposes an fd a selector can monitor, plus a
non-blocking read. Endpoints layer framing on top. Lives in visio-schema so a
schema-only user can read/write one stream with no bus.

`FdLink` works equally well for `os.openpty()` pairs (tests + the interop
harness) and for real serial fds opened against `/dev/ttyUSB*` / `/dev/ttyGS0` —
the same blocking-write + selector-gated-read pattern applies.
"""
from __future__ import annotations

import os
import termios
import tty
from abc import ABC, abstractmethod


class Link(ABC):
    """Abstract byte channel exposed to a fd-driven Endpoint."""

    @abstractmethod
    def fileno(self) -> int:
        """Return the fd the bus selector should monitor."""

    @abstractmethod
    def read_nonblocking(self, max_bytes: int) -> bytes:
        """Read up to `max_bytes`. Returns b"" on EOF. Raises BlockingIOError
        when no data is ready (fd is non-blocking)."""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write all bytes. Blocks if the kernel buffer is full. Raises
        BrokenPipeError if the link is closed/broken."""

    @abstractmethod
    def close(self) -> None:
        """Idempotent close."""


class FdLink(Link):
    """Raw-fd-backed Link. Works for pty pairs (tests), real serial ports
    (`/dev/ttyUSB*`), and anything else that gives you an fd with the standard
    POSIX read/write semantics. Reads are selector-gated; writes block on
    backpressure."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        # Leave the fd in blocking mode. Reads are gated by the bus selector
        # (we only call os.read when the selector reports readable, so it
        # returns immediately). Writes block when the kernel buffer is full —
        # the right backpressure: the bus loop pauses, the consumer drains, the
        # write resumes. Non-blocking writes busy-spin on BlockingIOError under
        # GIL pressure and corrupt frames at high throughput.
        _try_set_raw(fd)
        self._closed = False

    @staticmethod
    def pair() -> tuple[FdLink, FdLink]:
        """Return two FdLinks connected via os.openpty(). For tests + the
        cross-language interop harness. Real-fd users construct FdLink(fd)
        directly with their already-opened serial / socket / etc. fd."""
        master, slave = os.openpty()
        return FdLink(master), FdLink(slave)

    def fileno(self) -> int:
        return self._fd

    def read_nonblocking(self, max_bytes: int) -> bytes:
        if self._closed:
            return b""
        # fd is blocking, but the bus only calls this after the selector
        # reports readable, so it returns immediately with up to max_bytes.
        return os.read(self._fd, max_bytes)

    def write(self, data: bytes) -> None:
        if self._closed:
            raise BrokenPipeError("FdLink closed")
        # os.write may return a short count on signal interruption or partial
        # kernel write. Loop until everything is accepted — silently dropping
        # the tail would corrupt frames.
        view = memoryview(data)
        while view:
            n = os.write(self._fd, view)
            if n <= 0:
                raise BrokenPipeError("FdLink: os.write returned 0")
            view = view[n:]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._fd)
        except OSError:
            pass


def _try_set_raw(fd: int) -> None:
    """Put a tty/pty fd in raw mode. Best-effort on non-tty fds (which
    `termios` refuses with ENOTTY); other fd types are left untouched."""
    try:
        tty.setraw(fd, termios.TCSANOW)
    except termios.error:
        pass
