"""The test that proves the actual fix: the native reader drains the CDC-ACM
kernel buffer GIL-free, so a CPU-bound Python thread holding the GIL cannot make
it lose bytes.

We write a burst larger than the pty kernel buffer, then deliberately hold the
GIL in a pure-Python busy loop WITHOUT draining (no poll_batch). A pure-Python
reader would be starved here and the kernel buffer would overflow, dropping bytes
(surfacing as CRC errors / lost frames). The native C++ reader thread runs without
the GIL, so it keeps draining into its (large) inbox; afterwards every frame is
present with zero drops.

Skipped when the native extension isn't built.
"""
from __future__ import annotations

import os
import time
import tty

import pytest

from visio_schema.transport.framing import frame_bytes
from visio_schema.wire.message import Message

_creader = pytest.importorskip("visio_schema._creader")

N_FRAMES = 1500          # ~75 KB total — well over a typical pty kernel buffer
PAYLOAD = b"\xAB" * 40


def _spin_holding_gil(seconds: float) -> None:
    # A pure-Python loop never releases the GIL between bytecodes for long, so a
    # GIL-bound reader thread would starve for this whole window.
    end = time.monotonic() + seconds
    x = 0
    while time.monotonic() < end:
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF


pytestmark = pytest.mark.pty  # reads a live pty via the native reader — see tests/conftest.py


def test_reader_does_not_drop_under_gil_starvation() -> None:
    master, slave = os.openpty()
    tty.setraw(slave)
    path = os.ttyname(slave)

    reader = _creader.Reader(path, 1_000_000)  # large inbox: no policy drops
    reader.start()
    time.sleep(0.1)  # let it open the slave

    expected = []
    buf = bytearray()
    for i in range(N_FRAMES):
        m = Message(stream_id=16 + (i % 8), payload=PAYLOAD, seq=i)
        m.timestamp.FromNanoseconds(1_000_000_000 + i)
        expected.append((m.stream_id, i))
        buf += frame_bytes(m)

    # Write the whole burst, then hold the GIL busy WITHOUT draining. The native
    # reader thread (GIL-free) must keep the kernel buffer drained the whole time.
    os.write(master, bytes(buf))
    _spin_holding_gil(0.6)

    got = []
    deadline = time.time() + 5.0
    while len(got) < N_FRAMES and time.time() < deadline:
        for f in reader.poll_batch(200, 0):
            got.append((f.stream_id, f.seq))
            assert bytes(f.payload) == PAYLOAD  # no corruption => no kernel overflow

    dropped = reader.dropped()
    reader.stop()

    assert dropped == 0, f"inbox dropped {dropped} frames"
    assert got == expected, f"expected {N_FRAMES} frames in order, got {len(got)}"
