"""fd byte I/O — the raw file-descriptor layer beneath endpoints.

There is no Link object: the fd IS the link. An endpoint owns one fd and does its
own non-blocking poll/read/write through these helpers; a reopenable endpoint gets
a fresh fd from an ``FdFactory`` (``Callable[[], int]``, -1 on failure) on each
reconnect. Mirrors the C++ ``link.hpp`` fd helpers.

Works for ``os.openpty()`` pairs (tests + the interop harness) and real serial
fds (``/dev/ttyUSB*`` / ``/dev/ttyGS0``) alike.
"""
from __future__ import annotations

import os
import termios
import tty
from collections.abc import Callable

# A source of fresh, connected fds for a reopenable endpoint (-1 on failure).
FdFactory = Callable[[], int]


def set_nonblocking(fd: int) -> None:
    """Set the fd non-blocking (so read_some/write_some never block)."""
    os.set_blocking(fd, False)


def set_raw_mode(fd: int) -> None:
    """Best-effort raw tty mode. No-op on non-ttys (termios refuses with ENOTTY)."""
    try:
        tty.setraw(fd, termios.TCSANOW)
    except termios.error:
        pass


def read_some(fd: int, max_bytes: int) -> bytes | None:
    """One non-blocking read. Returns the bytes read (len>0), ``b""`` on
    would-block (EAGAIN — no data yet, NOT EOF), or ``None`` on EOF / a dead fd."""
    try:
        chunk = os.read(fd, max_bytes)
    except BlockingIOError:
        return b""
    except OSError:
        return None
    if not chunk:
        return None  # EOF
    return chunk


def write_some(fd: int, data: bytes) -> int:
    """One non-blocking write. Returns bytes accepted (0..len), 0 on would-block
    (EAGAIN), or -1 on a broken fd. Mirrors ::write."""
    try:
        return os.write(fd, data)
    except BlockingIOError:
        return 0
    except (BrokenPipeError, OSError):
        return -1


def close_fd(fd: int) -> None:
    """Close ``fd`` (no-op on < 0). Flushes any queued TX first so a CDC-ACM gadget
    close doesn't block when no host is reading; no-op (ENOTTY) on non-ttys."""
    if fd < 0:
        return
    try:
        termios.tcflush(fd, termios.TCOFLUSH)
    except (termios.error, OSError):
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def open_serial_fd(path: str) -> int:
    """Open a device path raw + non-blocking. Returns the fd or -1 on failure."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError:
        return -1
    set_raw_mode(fd)
    set_nonblocking(fd)
    return fd


def make_fd_pair() -> tuple[int, int]:
    """A connected pair of raw, non-blocking pty fds (master, slave). For tests
    and the cross-language interop harness: one end drives an endpoint, the other
    is read/written directly (or handed to the C++ peer by slave path)."""
    master, slave = os.openpty()
    for fd in (master, slave):
        set_raw_mode(fd)
        set_nonblocking(fd)
    return master, slave
