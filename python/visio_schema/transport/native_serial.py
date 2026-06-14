"""NativeSerialEndpoint — a drop-in Endpoint backed by the native (`_creader`)
GIL-free reader, with a pure-Python fallback.

The native C++ SerialEndpoint reads + deframes on its own thread with the GIL
released and pushes decoded frames into a bounded queue; this endpoint's drain
thread pulls batches (``poll_batch``) and dispatches ``on_inbound`` to the Bus on
Python's own schedule. So a CPU-bound consumer (render/numpy/PyAV) can stall the
*dispatch* without ever stalling the *read* — the queue absorbs the backlog
instead of the CDC-ACM kernel buffer overflowing and dropping bytes. The wire
framing + nanopb Header run in C++ (reused from the firmware sources); protobuf
payloads and MCAP stay in Python.

If the native extension isn't importable (no toolchain at install, or
``VISIO_NO_NATIVE=1``), callers fall back to the pure-Python
:class:`~visio_schema.transport.serial.SerialEndpoint` — selected by
:func:`visio_schema.transport.serial_endpoint`. The two satisfy the same
``Endpoint`` ABC, so the Bus is unchanged.
"""
from __future__ import annotations

import threading

from google.protobuf.timestamp_pb2 import Timestamp

from visio_schema.transport.endpoint import ClosedFn, Endpoint, InboundFn
from visio_schema.wire.message import Message

try:
    from visio_schema import _creader

    HAVE_NATIVE = True
except ImportError:  # pragma: no cover - exercised on no-toolchain installs
    _creader = None  # type: ignore[assignment]
    HAVE_NATIVE = False


class NativeSerialEndpoint(Endpoint):
    """Serial Endpoint whose read+deframe runs GIL-free in the native reader.

    Reopenable (path-based, watchdog) like ``SerialEndpoint(path)``: a transient
    CDC-ACM drop self-heals, so ``on_closed`` is not fired (it is reserved for the
    fixed-fd mode). ``max_depth`` bounds the inbound queue (drop-oldest on a
    sustained-stall consumer; ``dropped`` counts shed frames).
    """

    def __init__(self, path: str, *, max_depth: int = 4096,
                 poll_timeout_ms: int = 200) -> None:
        if not HAVE_NATIVE:
            raise RuntimeError("visio_schema._creader is not available")
        self._path = path
        self._max_depth = max_depth
        self._poll_timeout_ms = poll_timeout_ms
        self._reader = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_inbound: InboundFn | None = None
        self._dropped = 0

    def start(self, on_inbound: InboundFn | None, on_closed: ClosedFn | None) -> None:
        # on_closed is accepted for the Endpoint contract but never fired: this is
        # a reopenable endpoint, which self-heals across a link drop.
        self._on_inbound = on_inbound
        self._stop.clear()
        self._reader = _creader.Reader(self._path, self._max_depth)
        self._reader.start()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def send(self, msg: Message) -> None:
        if self._reader is not None:
            self._reader.send(msg.stream_id, msg.seq,
                              msg.timestamp.ToNanoseconds(), bytes(msg.payload))

    def stop(self) -> None:
        self._stop.set()
        if self._reader is not None:
            self._dropped = self._reader.dropped()  # keep the count past teardown
            self._reader.stop()  # closes the inbox (wakes poll_batch) + joins reader
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self._reader = None

    @property
    def dropped(self) -> int:
        return self._reader.dropped() if self._reader is not None else self._dropped

    def _drain(self) -> None:
        reader = self._reader
        while not self._stop.is_set():
            batch = reader.poll_batch(self._poll_timeout_ms, 0)
            if self._on_inbound is None:
                continue
            for f in batch:
                # payload is a zero-copy memoryview pinned to the frame; the only
                # per-frame alloc (on this starvable drain thread, not the reader
                # thread) is the Timestamp.
                ts = Timestamp()
                ts.FromNanoseconds(f.ts_ns)
                self._on_inbound(Message(stream_id=f.stream_id, payload=f.payload,
                                         seq=f.seq, timestamp=ts), self)
