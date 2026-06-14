"""McapReaderEndpoint — replay a recorded MCAP as a live-looking source ACTIVE OBJECT.

The replay counterpart of :class:`~visio_schema.mcap.writer_endpoint.McapWriterEndpoint`:
it turns a recorded ``.mcap`` into an :class:`Endpoint` you can drop in where a live
:class:`~visio_schema.transport.serial.SerialEndpoint` used to be, so a downstream
``Bus`` / :class:`ChannelRegistry` consumer is unchanged.

**Virtual device.** A recording stores each channel's topic + schema but not the
device's original stream ids (MCAP assigns its own small channel ids, which collide
with the control-id range). So a faithful replay behaves like a fresh device: it
declares the MCAP's channels into its own :class:`ChannelRegistry` (ids
``>= FIRST_DYNAMIC``), emits a synthesized ``DeviceInfo`` announce the first time each
channel is seen, then streams data on the declared id. A bus learns the channels from
the announce exactly as it would from a live link, then resolves the data.

**Pacing.** ``speed`` controls playback:
  * ``1.0`` (default) — realtime: sleep so inter-message gaps match the recorded
    ``log_time`` (which ``read_mcap`` already yields in ascending order).
  * ``None`` — as fast as the (synchronous) ``on_inbound`` consumer accepts.
  * ``> 1`` faster, ``< 1`` slower (e.g. ``2.0`` = 2x, ``0.5`` = half speed).

``mcap`` is an optional dependency (``pip install visio-schema[mcap]``), imported
lazily by :func:`read_mcap`.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from visio_schema.mcap.reader import read_mcap
from visio_schema.routing.registry import DEVICE_INFO_TOPIC, ChannelRegistry
from visio_schema.transport.endpoint import ClosedFn, Endpoint, InboundFn
from visio_schema.wire.control import DEVICE_INFO
from visio_schema.wire.message import Message

__all__ = ["McapReaderEndpoint"]


class McapReaderEndpoint(Endpoint):
    """Read-only source Endpoint that replays an MCAP recording on its own thread."""

    def __init__(
        self,
        path: str | Path,
        *,
        speed: float | None = 1.0,
        device_name: str = "replay",
    ) -> None:
        if speed is not None and speed <= 0:
            raise ValueError("speed must be a positive float, or None for as-fast-as-possible")
        self._path = path
        self._speed = speed
        self._device_name = device_name
        self._on_inbound: InboundFn | None = None
        self._on_closed: ClosedFn | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, on_inbound: InboundFn | None,
              on_closed: ClosedFn | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("McapReaderEndpoint.start() called while already replaying")
        self._on_inbound = on_inbound
        self._on_closed = on_closed
        self._stop.clear()
        self._thread = threading.Thread(target=self._replay_loop, daemon=True)
        self._thread.start()

    def send(self, msg: Message) -> None:
        pass  # read-only source: a replay never sends (symmetric to the write-only sink)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _replay_loop(self) -> None:
        reg = ChannelRegistry(device_name=self._device_name)
        seen: set[str] = set()
        t0_wall: float | None = None
        t0_msg: int | None = None
        pairs = read_mcap(self._path)            # (Message, Channel), ascending log_time
        try:
            for msg, ch in pairs:
                if self._stop.is_set():
                    return
                if ch.topic == DEVICE_INFO_TOPIC:
                    continue                     # we synthesize our own announce; never replay one
                if ch.topic not in seen:
                    seen.add(ch.topic)
                    reg.declare(ch.topic, ch.schema_name, ch.schema,
                                encoding=ch.encoding, schema_encoding=ch.schema_encoding)
                    # a bus drops a channel's data until it learns the channel from an announce
                    self._on_inbound(Message(stream_id=DEVICE_INFO,
                                             payload=reg.self_info().SerializeToString()), self)
                ts_ns = msg.timestamp.ToNanoseconds()
                if self._speed is not None:
                    if t0_wall is None:
                        t0_wall, t0_msg = time.monotonic(), ts_ns
                    else:
                        delay = (t0_wall + (ts_ns - t0_msg) / 1e9 / self._speed
                                 - time.monotonic())
                        if delay > 0 and self._stop.wait(delay):
                            return
                self._on_inbound(Message(stream_id=reg.local_id_for(ch.topic), payload=msg.payload,
                                         seq=msg.seq, timestamp=msg.timestamp), self)
        finally:
            pairs.close()
        if not self._stop.is_set() and self._on_closed is not None:
            self._on_closed(self)
