"""SerialEndpoint(path=...) opens a device node once into a fixed link.

The path ctor is sugar for "open this node, then behave exactly like the fixed-fd
ctor" — no factory, no auto-reconnect. A bad path raises rather than yielding a
silently-dead endpoint.
"""
from __future__ import annotations

import os
import time

import pytest

from visio_schema.transport import SerialEndpoint, make_fd_pair
from visio_schema.transport.endpoint import EndpointClosed
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
