"""Endpoints surface disconnects as EndpointClosed (they never self-reconnect).

The bus (or an app loop) catches it and decides what to do; the endpoint just
reports the broken link.
"""
from __future__ import annotations

import pytest

from visio_schema.transport import EndpointClosed, FdLink, SerialEndpoint
from visio_schema.wire.message import Message


def test_eof_on_read_raises_endpoint_closed() -> None:
    a, b = FdLink.pair()
    rx = SerialEndpoint(b)
    a.close()                       # peer hangs up
    with pytest.raises(EndpointClosed):
        list(rx.try_read())


def test_broken_write_raises_endpoint_closed() -> None:
    a, _b = FdLink.pair()
    tx = SerialEndpoint(a)
    a.close()                       # link gone
    with pytest.raises(EndpointClosed):
        tx.write(Message(stream_id=16, payload=b"x"))
