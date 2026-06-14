"""McapReaderEndpoint — replay a recording as a virtual device.

It behaves like a live link: each channel is announced (synthesized DeviceInfo)
before its first data message, and data rides declared ids (>= FIRST_DYNAMIC), so a
bus learns + resolves it exactly as from a real device. ``speed`` paces playback
(1.0 realtime, None as-fast-as-possible).
"""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("mcap", reason="mcap library not installed")

from visio_schema.mcap import McapReaderEndpoint, McapWriter
from visio_schema.routing import FIRST_DYNAMIC, Channel, make_channel
from visio_schema.v1.service.device_info.device_info_pb2 import DeviceInfo
from visio_schema.wire.control import DEVICE_INFO
from visio_schema.wire.message import Message

_IMU = "visio_schema.v1.sensor.ImuRaw"
_TOPICS = ("/dev/imus/0/raw", "/dev/imus/1/raw")
_N = 4  # messages per channel


def _channel(cid: int, topic: str) -> Channel:
    return make_channel(topic, _IMU, stream_id=cid)


def _write_recording(path, *, span_ns: int) -> int:
    """Two channels, interleaved, timestamps spanning [base, base+span_ns]. The
    recorded ids are arbitrary — the replay re-declares its own. Returns #data msgs."""
    chans = [_channel(FIRST_DYNAMIC + i, t) for i, t in enumerate(_TOPICS)]
    base = 1_700_000_000_000_000_000
    with McapWriter(path) as w:
        for i in range(_N):
            ts = base + (span_ns * i) // (_N - 1)
            for ci, ch in enumerate(chans):
                m = Message(stream_id=ch.id, payload=f"{ci}-{i}".encode(), seq=i)
                m.timestamp.FromNanoseconds(ts)
                w.write(m, ch)
    return _N * len(_TOPICS)


class _Collector:
    def __init__(self) -> None:
        self.msgs: list[Message] = []
        self.closed = threading.Event()
        self.closed_count = 0
        self._lock = threading.Lock()

    def on_inbound(self, msg: Message, ep) -> None:
        with self._lock:
            self.msgs.append(msg)

    def on_closed(self, ep) -> None:
        self.closed_count += 1
        self.closed.set()


def _drive(path, speed) -> tuple[_Collector, float]:
    c = _Collector()
    ep = McapReaderEndpoint(path, speed=speed)
    t0 = time.monotonic()
    ep.start(c.on_inbound, c.on_closed)
    assert c.closed.wait(timeout=10.0), "replay did not finish"
    elapsed = time.monotonic() - t0
    ep.stop()
    return c, elapsed


def test_replay_announces_each_channel_before_its_data(tmp_path) -> None:
    path = tmp_path / "rec.mcap"
    ndata = _write_recording(path, span_ns=0)   # equal timestamps -> no pacing needed
    c, _ = _drive(path, speed=None)

    # The first emission is a DeviceInfo announce (a virtual-device handshake).
    assert c.msgs[0].stream_id == DEVICE_INFO

    # Every data message was announced before it arrived (so a bus would resolve it),
    # and the announces collectively name both recorded topics.
    announced_ids: set[int] = set()
    announced_topics: set[str] = set()
    data: list[Message] = []
    for m in c.msgs:
        if m.stream_id == DEVICE_INFO:
            di = DeviceInfo()
            di.ParseFromString(m.payload)
            announced_ids |= {ch.id for ch in di.channels}
            announced_topics |= {ch.topic for ch in di.channels}
        else:
            assert m.stream_id >= FIRST_DYNAMIC          # never the control range
            assert m.stream_id in announced_ids          # bus could map it
            data.append(m)

    assert announced_topics == set(_TOPICS)
    assert len(data) == ndata
    assert {m.payload for m in data} == {
        f"{ci}-{i}".encode() for ci in range(len(_TOPICS)) for i in range(_N)
    }
    assert c.closed_count == 1                            # on_closed fires once at EOF


def test_realtime_paces_slower_than_asap(tmp_path) -> None:
    path = tmp_path / "rec.mcap"
    _write_recording(path, span_ns=200_000_000)          # 200 ms recorded span

    _, asap = _drive(path, speed=None)
    _, realtime = _drive(path, speed=1.0)

    assert asap < 0.1                                     # asap dumps near-instantly
    assert realtime >= 0.15                              # ~ the 0.2 s span (tolerant)
    assert realtime > asap


def test_seq_and_timestamp_preserved(tmp_path) -> None:
    path = tmp_path / "rec.mcap"
    _write_recording(path, span_ns=0)              # all stamped at `base`
    c, _ = _drive(path, speed=None)

    base = 1_700_000_000_000_000_000
    data = [m for m in c.msgs if m.stream_id != DEVICE_INFO]
    assert sorted(m.seq for m in data) == sorted(list(range(_N)) * len(_TOPICS))
    assert all(m.timestamp.ToNanoseconds() == base for m in data)


def test_stop_interrupts_realtime_replay(tmp_path) -> None:
    path = tmp_path / "rec.mcap"
    _write_recording(path, span_ns=5_000_000_000)  # 5 s span — blocks for ~5 s if not interrupted
    c = _Collector()
    ep = McapReaderEndpoint(path, speed=1.0)
    t0 = time.monotonic()
    ep.start(c.on_inbound, c.on_closed)
    time.sleep(0.1)
    ep.stop()                                      # break the realtime sleep mid-replay
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0                           # stop() preempted the ~5 s pacing sleep
    assert c.closed_count == 0                     # interrupted, not EOF → on_closed must NOT fire


def test_empty_recording_closes_cleanly(tmp_path) -> None:
    path = tmp_path / "empty.mcap"
    with McapWriter(path):
        pass                                       # a valid MCAP with no messages
    c, _ = _drive(path, speed=None)

    assert c.msgs == []                            # nothing to announce, nothing to stream
    assert c.closed_count == 1                     # clean EOF


def test_bad_speed_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        McapReaderEndpoint(tmp_path / "x.mcap", speed=0)
    with pytest.raises(ValueError):
        McapReaderEndpoint(tmp_path / "x.mcap", speed=-1.0)
