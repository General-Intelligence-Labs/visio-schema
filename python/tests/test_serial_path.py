"""SerialEndpoint(path=...) opens a device node once into a fixed link.

The path ctor is sugar for "open this node, then behave exactly like the fixed-fd
ctor" — no factory, no auto-reconnect. A bad path raises rather than yielding a
silently-dead endpoint.
"""
from __future__ import annotations

import os
import termios
import time
import tty

import pytest

from visio_schema.transport import SerialEndpoint, make_fd_pair
from visio_schema.transport.endpoint import EndpointClosed
from visio_schema.transport.link import (
    open_serial_fd,
    read_some,
    set_raw_mode,
)
from visio_schema.wire.message import Message


def test_path_opens_and_streams() -> None:
    # A pty: drive one end via SerialEndpoint(path=<slave node>), read the master.
    master, slave = make_fd_pair()
    tx = SerialEndpoint(path=os.ttyname(slave))
    try:
        tx.start(None, None)
        tx.send(Message(stream_id=16, payload=b"hello"))
        got = b""
        for _ in range(100):                       # ~1s budget for the I/O thread
            try:
                got += os.read(master, 4096)       # master is non-blocking
            except BlockingIOError:
                pass
            if b"hello" in got:
                break
            time.sleep(0.01)
        assert b"hello" in got                     # framed bytes reached the wire
    finally:
        tx.stop()
        os.close(master)
        os.close(slave)


def test_path_bad_node_raises() -> None:
    # No silently-dead endpoint: an unopenable path is a hard construction error.
    with pytest.raises(EndpointClosed):
        SerialEndpoint(path="/dev/does-not-exist-visio")


def test_requires_exactly_one_of_fd_or_path() -> None:
    a, b = make_fd_pair()
    try:
        with pytest.raises(ValueError):
            SerialEndpoint()                       # neither
        with pytest.raises(ValueError):
            SerialEndpoint(a, path=os.ttyname(b))  # both
    finally:
        os.close(a)
        os.close(b)


@pytest.mark.pty
@pytest.mark.parametrize("hangup", [False, True])
def test_open_sets_local(hangup: bool) -> None:
    master, slave = os.openpty()
    opened = -1
    try:
        tty.setraw(slave)
        attrs = termios.tcgetattr(slave)
        attrs[2] &= ~termios.CLOCAL
        if hangup:
            attrs[2] |= termios.HUPCL
        else:
            attrs[2] &= ~termios.HUPCL
        attrs[2] |= getattr(termios, "CRTSCTS", 0)
        attrs[4] = attrs[5] = termios.B38400
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        expected = termios.tcgetattr(slave)

        # Generic raw mode keeps its carrier semantics.
        set_raw_mode(slave)
        assert termios.tcgetattr(slave) == expected
        assert not expected[2] & termios.CLOCAL

        opened = open_serial_fd(os.ttyname(slave))
        assert opened >= 0
        actual = termios.tcgetattr(opened)
        assert actual[2] & termios.CLOCAL
        expected[2] |= termios.CLOCAL
        assert actual == expected
        assert not os.get_blocking(opened)

        os.close(master)
        master = -1
        assert read_some(opened, 16) is None
    finally:
        if opened >= 0:
            os.close(opened)
        if master >= 0:
            os.close(master)
        os.close(slave)
