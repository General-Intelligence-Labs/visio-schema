"""A fixed-fd endpoint reports a disconnect via the on_closed callback (it never
self-reconnects); a write to a broken link sheds without raising. Active-object
endpoints own their I/O thread, so EOF surfaces as a callback, not a raise.
"""
from __future__ import annotations

import threading
import time

from visio_schema.transport import SerialEndpoint, close_fd, make_fd_pair
from visio_schema.wire.message import Message


def test_eof_reports_closed() -> None:
    a, b = make_fd_pair()
    rx = SerialEndpoint(b)
    closed = threading.Event()
    rx.start(None, lambda _ep: closed.set())
    close_fd(a)                        # peer hangs up
    assert closed.wait(timeout=2.0)    # on_closed fires once on EOF
    rx.stop()


def test_broken_write_sheds_without_raising() -> None:
    a, b = make_fd_pair()
    tx = SerialEndpoint(a)
    close_fd(b)                        # peer gone -> writes to `a` will EPIPE
    tx.start(None, None)
    tx.send(Message(stream_id=16, payload=b"x"))  # non-blocking enqueue; sheds
    time.sleep(0.05)                   # let the I/O thread attempt the write
    tx.stop()                          # reaching here without raising == pass
