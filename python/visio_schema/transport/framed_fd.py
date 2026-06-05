"""FramedFdEndpoint — COBS-delimited core-frames over a raw fd, as a self-threaded
ACTIVE OBJECT. ``start()`` spawns one I/O thread that polls the fd, drains the
bounded outbox (non-blocking writes), reads + decodes inbound frames (delivered
via ``on_inbound``), and reopens the fd (``tick``). ``send()`` is a thread-safe,
non-blocking enqueue. Mirrors the C++ ``FramedFdEndpoint``.

  - fixed fd (factory None): a read EOF/dead fd reports ``on_closed(self)`` once
    and the I/O thread exits; the owner detaches it.
  - reopenable (factory set): a dead fd is dropped and ``tick()`` reopens it with
    backoff. Self-heals; never calls on_closed.
"""
from __future__ import annotations

import os
import select
import threading
import time
from collections import deque

from visio_schema.transport.endpoint import ClosedFn, Endpoint, InboundFn
from visio_schema.transport.framing import extract_frames, frame_bytes
from visio_schema.transport.link import (
    FdFactory,
    close_fd,
    read_some,
    set_nonblocking,
    write_some,
)
from visio_schema.wire.message import Message

_TICK_S = 0.2                      # reopen cadence
_REOPEN_BACKOFF_NS = 500_000_000
_DEFAULT_OUTBOX_FRAMES = 4096      # drop-oldest bound (a stalled reader sheds)


class FramedFdEndpoint(Endpoint):
    """COBS-framed core-frame active object over a raw fd."""

    def __init__(
        self,
        fd: int | None = None,
        *,
        factory: FdFactory | None = None,
        reopen_backoff_ns: int = _REOPEN_BACKOFF_NS,
        max_outbox_frames: int = _DEFAULT_OUTBOX_FRAMES,
    ) -> None:
        self._factory = factory
        self._reopen_backoff_ns = reopen_backoff_ns
        self._next_reopen_ns = 0
        self._max_outbox = max_outbox_frames
        self._fd = -1
        if factory is not None:
            self._adopt(factory())
        elif fd is not None:
            self._adopt(fd)

        self._outbox: deque[bytes] = deque()   # framed bytes; producer/dispatch side
        self._outbox_lock = threading.Lock()
        self._outbox_bytes = 0
        self._dropped = 0
        self._inflight = b""                   # drainer-private (I/O thread)
        self._rx = bytearray()                 # drainer-private
        self._wake_r = -1
        self._wake_w = -1
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_inbound: InboundFn | None = None
        self._on_closed: ClosedFn | None = None

    # ── Endpoint API ────────────────────────────────────────────────────

    def start(self, on_inbound: InboundFn | None, on_closed: ClosedFn | None) -> None:
        self._on_inbound = on_inbound
        self._on_closed = on_closed
        if self._wake_r < 0:
            self._wake_r, self._wake_w = os.pipe()
            set_nonblocking(self._wake_r)
            set_nonblocking(self._wake_w)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def send(self, msg: Message) -> None:
        framed = frame_bytes(msg)
        with self._outbox_lock:
            if len(self._outbox) >= self._max_outbox:
                old = self._outbox.popleft()       # drop-oldest
                self._outbox_bytes -= len(old)
                self._dropped += 1
            self._outbox.append(framed)
            self._outbox_bytes += len(framed)
        self._wake()

    def stop(self) -> None:
        self._stop.set()
        self._wake()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if self._fd >= 0:
            close_fd(self._fd)
            self._fd = -1
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1

    # ── Diagnostics (thread-safe) ───────────────────────────────────────

    @property
    def link_up(self) -> bool:
        return self._fd >= 0

    def pending_bytes(self) -> int:
        with self._outbox_lock:
            return self._outbox_bytes + (len(self._inflight))

    def dropped(self) -> int:
        return self._dropped

    # ── Reopen hooks (I/O thread). SerialEndpoint may override tick(). ───

    def tick(self, now_ns: int) -> None:
        """Called from the I/O thread each loop. Base: reopen a down fd with
        backoff. Subclasses override to add a watchdog."""
        if self._fd >= 0 or self._factory is None:
            return
        if now_ns < self._next_reopen_ns:
            return
        if not self._reopen():
            self._next_reopen_ns = now_ns + self._reopen_backoff_ns

    def _mark_link_dead(self) -> None:
        if self._fd >= 0:
            close_fd(self._fd)
            self._fd = -1
        with self._outbox_lock:
            self._outbox.clear()       # a fresh reader would desync on a half-frame
            self._outbox_bytes = 0
        self._inflight = b""
        self._rx.clear()
        self._next_reopen_ns = 0       # reopen ASAP on the next tick

    def _reopen(self) -> bool:
        if self._factory is None:
            return False
        fd = self._factory()
        if fd is not None and fd >= 0:
            self._adopt(fd)
            self._rx.clear()
        return self._fd >= 0

    # ── Internals ───────────────────────────────────────────────────────

    def _adopt(self, fd: int | None) -> None:
        if fd is None or fd < 0:
            self._fd = -1
            return
        set_nonblocking(fd)
        self._fd = fd

    def _wake(self) -> None:
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b"\x01")
            except (BlockingIOError, OSError):
                pass

    def _has_pending(self) -> bool:
        if self._inflight:
            return True
        with self._outbox_lock:
            return bool(self._outbox)

    def _loop(self) -> None:
        while not self._stop.is_set():
            fd = self._fd
            rlist = [self._wake_r]
            wlist: list[int] = []
            if fd >= 0:
                rlist.append(fd)
                if self._has_pending():
                    wlist.append(fd)
            try:
                r, _w, _x = select.select(rlist, wlist, [], _TICK_S)
            except (OSError, ValueError):
                r = []
            if self._wake_r in r:
                try:
                    while os.read(self._wake_r, 4096):
                        pass
                except (BlockingIOError, OSError):
                    pass
            if self._stop.is_set():
                break

            self._pump()  # drain outbox (no-op if fd down / nothing pending)

            if fd >= 0 and fd in r:
                self._read_inbound(fd)

            self.tick(time.monotonic_ns())

    def _pump(self) -> None:
        fd = self._fd
        if fd < 0:
            return
        while True:
            if self._inflight:
                n = write_some(fd, self._inflight)
                if n < 0:
                    self._mark_link_dead()
                    return
                if n == 0:
                    return                       # EAGAIN — retry on next POLLOUT
                self._inflight = self._inflight[n:]
                if self._inflight:
                    return                       # partial — wait for POLLOUT
            with self._outbox_lock:
                if not self._outbox:
                    return
                self._inflight = self._outbox.popleft()
                self._outbox_bytes -= len(self._inflight)

    def _read_inbound(self, fd: int) -> None:
        chunk = read_some(fd, 4096)
        if chunk is None:                        # EOF / dead fd
            if self._factory is not None:
                self._mark_link_dead()           # reopenable: self-heal on next tick
            else:
                if self._on_closed is not None:
                    self._on_closed(self)        # fixed fd: owner detaches us
                self._stop.set()                 # thread exits
            return
        if chunk:
            self._rx.extend(chunk)
            for msg in extract_frames(self._rx):
                if self._on_inbound is not None:
                    self._on_inbound(msg, self)
