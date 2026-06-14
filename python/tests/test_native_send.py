"""Native send() path. NativeSerialEndpoint frames + writes a Message byte-for-byte
identically to the pure-Python codec, and the bytes decode back to the same fields
— the send-side counterpart to the receive-side parity gate.
"""
from __future__ import annotations

import os
import select
import time
import tty

import pytest

from visio_schema.transport.framing import extract_frames, frame_bytes
from visio_schema.wire.message import Message

pytest.importorskip("visio_schema._creader")

from visio_schema.transport import NativeSerialEndpoint


def _read_for(fd: int, seconds: float) -> bytes:
    buf = bytearray()
    deadline = time.time() + seconds
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            chunk = os.read(fd, 65536)
            if chunk:
                buf += chunk
    return bytes(buf)


def test_native_send_matches_pure_framing_and_roundtrips() -> None:
    master, slave = os.openpty()
    tty.setraw(slave)  # raw line discipline governs both directions
    path = os.ttyname(slave)

    ep = NativeSerialEndpoint(path)
    ep.start(None, None)
    time.sleep(0.1)  # let the reader open the slave

    m = Message(stream_id=21, payload=b"cmd-payload", seq=99)
    m.timestamp.FromNanoseconds(1_234_567_890_123)
    ep.send(m)

    raw = _read_for(master, 0.5)
    ep.stop()

    # Byte-identical to the pure-Python framing of the same message.
    assert raw == frame_bytes(m)
    # And decodes back to the same fields.
    msgs = extract_frames(bytearray(raw))
    assert len(msgs) == 1
    got = msgs[0]
    assert (got.stream_id, got.seq, got.timestamp.ToNanoseconds(), got.payload) == (
        21, 99, 1_234_567_890_123, b"cmd-payload")
