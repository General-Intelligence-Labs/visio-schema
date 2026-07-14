"""Tests for the pull sink ``QueueEndpoint`` (visio_schema.transport.queue).

Covers the upgraded surface — the blocking ``pop_batch(timeout)`` and the bounded
drop-oldest ``max_depth`` + ``dropped`` counter — that backs the native pull API's
Python twin. (The C++ twin is covered by the visio C++ suite.)
"""
from __future__ import annotations

import threading
import time

from visio_schema.transport.queue import QueueEndpoint
from visio_schema.wire.message import Message


def _msg(sid: int = 16, payload: bytes = b"x") -> Message:
    return Message(stream_id=sid, payload=payload)


def test_pop_batch_returns_queued_immediately():
    q = QueueEndpoint()
    q.send(_msg())
    q.send(_msg())
    t0 = time.monotonic()
    out = q.pop_batch(500)
    assert len(out) == 2
    assert time.monotonic() - t0 < 0.1            # did not wait out the timeout


def test_pop_batch_times_out_to_empty():
    q = QueueEndpoint()
    t0 = time.monotonic()
    out = q.pop_batch(50)
    dt = time.monotonic() - t0
    assert out == []
    assert 0.03 <= dt < 1.0                        # actually blocked ~50 ms, didn't hang


def test_pop_batch_wakes_on_send():
    q = QueueEndpoint()

    def _producer():
        time.sleep(0.05)
        q.send(_msg())

    threading.Thread(target=_producer, daemon=True).start()
    out = q.pop_batch(2000)                         # blocks until the producer sends
    assert len(out) == 1


def test_max_depth_drops_oldest_and_counts():
    q = QueueEndpoint(max_depth=2)
    q.send(_msg(payload=b"a"))
    q.send(_msg(payload=b"b"))
    q.send(_msg(payload=b"c"))                      # over depth -> evicts "a"
    out = q.pop_all()
    assert [m.payload for m in out] == [b"b", b"c"]
    assert q.dropped == 1


def test_stream_filter_drops_nonmatching():
    q = QueueEndpoint(stream_filter=16)
    q.send(_msg(sid=16))
    q.send(_msg(sid=17))                            # filtered out
    assert len(q.pop_all()) == 1
