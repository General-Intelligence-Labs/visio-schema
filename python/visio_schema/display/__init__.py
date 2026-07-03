#!/usr/bin/env python3
"""Display / record live Visio data: Foxglove, Rerun, and/or MCAP.

Reads Visio messages from a **live serial port** or a **live TCP connection**
(both COBS-delimited core frames, framing.md §3.2) **or replays a recorded MCAP
file**, and fans them out to any combination of a **live Foxglove Studio**
WebSocket server, a **live Rerun** viewer, and an **MCAP recording**. Its serial,
Foxglove, Rerun, and H.265-decode dependencies ship with the `visio-schema`
package (``pip install visio-schema``) and are imported lazily.

Installed as the ``visio-display`` console command (also runnable as
``python -m visio_schema.display``):

    # live serial -> Rerun (spawns the viewer; auto-lays-out views)
    visio-display --serial /dev/ttyACM0 --rerun

    # live TCP (the device's preview listener, default port 9000) -> Rerun
    visio-display --tcp GILABS-1234.local --rerun

    # replay a recorded MCAP file into Rerun
    visio-display --mcap-in run.mcap --rerun

    # live serial -> Foxglove Studio (prints a URL to open)
    visio-display --serial /dev/ttyACM0 --foxglove

    # live TCP -> record an MCAP (and watch live at the same time)
    visio-display --tcp 10.0.0.7:9000 --out run.mcap --rerun

The source is exactly one of `--serial` / `--tcp` / `--mcap-in`. MCAP-file ->
Foxglove is not supported (open the file directly in Foxglove Studio, which
seeks/scrubs); the script says so and ignores `--foxglove` for a file source.

`--foxglove` starts a WebSocket *data source* server (not itself a viewer) and
prints a URL; open it in Foxglove Studio, or in Studio choose
Open connection → Foxglove WebSocket → ws://localhost:8765. A starter layout is
in `ego_layout.json` (Studio ▸ Layouts ▸ Import from file).

`--bitrate` (on by default whenever a Foxglove or MCAP sink is active) derives a
realtime sliding-window bitrate from the stream and publishes it as json on
`/stats/bitrate/_total` (whole link + a video-only subtotal) and one
`/stats/bitrate/<camera-topic>` per video stream — plot the `mbps` field in a
Foxglove Plot panel (the shipped layout includes one). It's viewer-derived (json,
not on the wire), so the device never sees it. Disable with `--no-bitrate`; tune
the window with `--bitrate-window SECS` (default 2.0).

`--rerun` spawns the Rerun viewer and drives an explicit layout (camera views,
a 3D IMU-orientation scene, IMU time-series). The Rerun rendering is a port of
an earlier Rerun renderer (H.265 decoded with PyAV to
images, orientation boxes, blueprint), only swapping the data model to
visio-schema. Needs `rerun-sdk` and `av`.

To look at an MCAP **file** (a recording, or one from make_sample_mcap.py),
just open it directly in Foxglove Studio: **File ▸ Open local file**. No need
to run this script for that.

Dynamic streams: the wire Header carries a compact `stream_id`, not a stream
type. Each device announces its channels (topic + schema) on the DeviceInfo
control stream (id CONTROL_STREAM_DEVICE_INFO); this reader keeps a local
`stream_id -> Channel` table from those announces and resolves each data frame
against it. A frame whose id hasn't been announced yet is dropped until it is
(drop-until-mapped). Over a direct point-to-point link the announced channel
ids are exactly the data-frame ids, so no remap is needed here.

Deliberately minimal — one read loop, no bus, no threads. The heavier,
bus-integrated transport lives in a separate bus/transport layer.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import selectors
import signal
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar, Protocol

from google.protobuf.timestamp_pb2 import Timestamp

# Stable public API — imported from the package root.
from visio_schema import (
    Channel,
    ChannelRegistry,
    McapWriter,
    Message,
    make_channel,
    read_mcap,
)
from visio_schema.foxglove.CompressedVideo_pb2 import CompressedVideo
from visio_schema.foxglove.FrameTransform_pb2 import FrameTransform

# Low-level fd helpers (advanced/internal) — used to feed messages into resolved() as a generator.
from visio_schema.transport import close_fd, extract_frames, read_some, set_nonblocking
from visio_schema.v1.ros.geometry_msgs.quaternion_pb2 import Quaternion
from visio_schema.v1.sensor.imu_raw_pb2 import ImuRaw
from visio_schema.v1.sensor.system_health_pb2 import SystemHealth

# Payload schema names dispatched on (== the protobuf full names on the wire).
_QUAT_SCHEMA = "visio_schema.v1.ros.geometry_msgs.Quaternion"
_VIDEO_SCHEMA = "foxglove.CompressedVideo"
_IMU_RAW_SCHEMA = "visio_schema.v1.sensor.ImuRaw"
_HEALTH_SCHEMA = "visio_schema.v1.sensor.SystemHealth"
# Starter Foxglove layout shipped beside this script (panels mirror the Rerun view).
_LAYOUT_PATH = Path(__file__).resolve().parent / "ego_layout.json"
# Synthetic stream id for the derived /tf channel — outside the wire id space so
# it can't collide with an announced stream.
_TF_STREAM_ID = 0x7F000001
# The device's live-preview TCP listener port — matches the `_umi-protocol._tcp`
# mDNS service and `port=9000` in the ego/glove .conf. Used when --tcp omits one.
_DEFAULT_TCP_PORT = 9000

# Derived realtime-bitrate channels (see BitrateDeriver). Synthetic stream ids in
# a reserved high range so they can't collide with announced ids or _TF_STREAM_ID:
# `/stats/bitrate/_total` uses the base itself (no source stream has id 0), each
# per-source channel uses base + the source's stream_id.
_BITRATE_STREAM_BASE = 0x7F010000
_BITRATE_TOPIC_PREFIX = "/stats/bitrate"
_BITRATE_TOTAL_TOPIC = f"{_BITRATE_TOPIC_PREFIX}/_total"
_BITRATE_SCHEMA_NAME = "visio.stats.Bitrate"
# json (not protobuf): bitrate is a viewer-derived metric, so it stays off the
# wire contract. A permissive JSON Schema is enough for Foxglove to offer the
# numeric fields as plot paths.
_BITRATE_SCHEMA = json.dumps({
    "type": "object",
    "title": "Bitrate",
    "description": "Realtime sliding-window bitrate of a Visio stream (or the whole link).",
    "properties": {
        "mbps": {"type": "number", "description": "delivered megabits/s over the window"},
        "fps": {"type": "number", "description": "delivered messages/s over the window"},
        "bytes": {"type": "integer", "description": "payload bytes summed over the window"},
        "drops": {"type": "integer", "description": "frames lost in window (per-stream seq gaps)"},
        "drop_pct": {"type": "number", "description": "percent of expected frames lost"},
        "video_mbps": {"type": "number", "description": "video-only subtotal (the _total channel)"},
    },
}).encode()
# How often (in *message-time* seconds) a bitrate sample is emitted. Message-time,
# not wall clock, so a live link and an MCAP replay produce identical numbers.
_BITRATE_EMIT_S = 0.5
# A per-stream `seq` jump larger than this is read as a reconnect/reset/wrap, not a
# real drop burst, so it doesn't spike the drop count. (seq is uint32, stamped at
# publish before the transmit outbox, so an ordinary lost frame is a small gap.)
_BITRATE_MAX_GAP = 10_000

# Set by SIGINT/SIGTERM so the read loop exits and the `finally` finalizes the
# MCAP. Handling SIGTERM (not just Ctrl-C) matters when this runs under a
# service manager or `timeout`/`kill` — otherwise the MCAP is left unfinalized.
_STOP = threading.Event()


# --------------------------------------------------------------------------- #
# Quaternion -> FrameTransform: make IMU orientation render in the 3D panel     #
# --------------------------------------------------------------------------- #
class TfDeriver:
    """Foxglove's 3D panel renders coordinate frames (``foxglove.FrameTransform``),
    not a bare ``Quaternion`` — so an IMU quat stream shows up only as raw fields.
    For each quat message, synthesize a ``world -> <imu>`` transform (rotation
    only) on a single ``/tf`` channel; the stock 3D panel then renders the IMU
    frame's axes rotating, no community panel needed. Sink-agnostic: the derived
    message rides the same ``sink.write(msg, ch)`` path as everything else."""

    def __init__(self) -> None:
        self._channel = make_channel(
            "/tf", "foxglove.FrameTransform", stream_id=_TF_STREAM_ID
        )

    def derive(self, msg: Message, ch: Channel) -> tuple[Message, Channel] | None:
        if ch.schema_name != _QUAT_SCHEMA:
            return None
        q = Quaternion()
        q.ParseFromString(msg.payload)
        ft = FrameTransform()
        ft.timestamp.CopyFrom(msg.timestamp)
        ft.parent_frame_id = "world"
        # "/gripper/imu/0/quat" -> child frame "gripper/imu/0"
        ft.child_frame_id = ch.topic.rsplit("/", 1)[0].strip("/")
        ft.rotation.x, ft.rotation.y, ft.rotation.z, ft.rotation.w = q.x, q.y, q.z, q.w
        out = Message(stream_id=_TF_STREAM_ID, payload=ft.SerializeToString(), seq=msg.seq)
        out.timestamp.CopyFrom(msg.timestamp)
        return out, self._channel


# --------------------------------------------------------------------------- #
# Realtime bitrate: make link/stream throughput plottable in Foxglove           #
# --------------------------------------------------------------------------- #
class BitrateDeriver:
    """Derive a realtime per-stream + total *bitrate* from the message flow and
    publish it on json ``/stats/bitrate/*`` channels Foxglove can plot.

    Sink-agnostic, like :class:`TfDeriver` — but instead of one output per input
    it accumulates payload bytes in a sliding window and emits a sample at most
    every :data:`_BITRATE_EMIT_S`. The clock is the *message* timestamp, not wall
    time, so a live link and an MCAP replay produce the same numbers (and a paused
    or replayed stream doesn't decay to zero). Each video stream gets its own
    ``/stats/bitrate/<topic>`` line — mirroring how each IMU gets its own plot —
    and ``/stats/bitrate/_total`` carries the whole link (every stream) plus a
    video-only subtotal.

    The channels are json (encoding ``json`` / schema ``jsonschema``): bitrate is
    viewer-derived, so it stays off the protobuf wire contract. The Foxglove and
    MCAP sinks are encoding-agnostic; the Rerun sink ignores the unknown schema.

    Each sample also carries a ``drops``/``drop_pct`` derived from gaps in the
    per-stream ``seq``: a frame lost to transmit backpressure is stamped before
    the device's outbox, so a dropped frame shows up as a missing seq. That
    separates a bitrate dip caused by *lost* frames (drops > 0) from one caused by
    *smaller* frames (drops 0, fps steady) — the difference between a saturated
    link and a low-complexity scene.
    """

    def __init__(self, window: float = 2.0) -> None:
        self._window = max(window, 0.1)
        # Don't emit faster than the window itself when the window is very small.
        self._emit_ns = int(min(_BITRATE_EMIT_S, self._window) * 1e9)
        self._events: collections.deque = collections.deque()  # (t_ns, stream_id, nbytes, dropped)
        self._video_topics: dict[int, str] = {}   # video source stream_id -> topic
        self._channels: dict[int, Channel] = {}    # derived stream_id -> json Channel
        self._last_seq: dict[int, int] = {}        # source stream_id -> last seen seq
        self._last_emit_ns: int | None = None
        self._seq = 0

    def feed(self, msg: Message, ch: Channel) -> list[tuple[Message, Channel]]:
        """Record one source message; return any bitrate samples due now (possibly
        several — one per video stream plus the total — or none)."""
        t = _ns(msg.timestamp)
        sid = msg.stream_id
        if ch.schema_name == _VIDEO_SCHEMA:
            self._video_topics.setdefault(sid, ch.topic)
        self._events.append((t, sid, len(msg.payload), self._gap(sid, msg.seq)))
        if self._last_emit_ns is None:
            self._last_emit_ns = t  # first message only primes the clock
            return []
        cutoff = t - int(self._window * 1e9)
        ev = self._events
        while ev and ev[0][0] < cutoff:  # device timestamps are ~monotonic per link
            ev.popleft()
        if t - self._last_emit_ns < self._emit_ns:
            return []
        self._last_emit_ns = t
        return self._emit(t)

    def _gap(self, sid: int, seq: int) -> int:
        """Frames missing between the last seq seen on `sid` and this one (0 on
        first sight, or on an implausibly large jump = reconnect/reset/wrap)."""
        prev = self._last_seq.get(sid)
        self._last_seq[sid] = seq
        if prev is None:
            return 0
        gap = (seq - prev - 1) & 0xFFFFFFFF  # seq is uint32
        return gap if 0 < gap <= _BITRATE_MAX_GAP else 0

    def _emit(self, t: int) -> list[tuple[Message, Channel]]:
        per_bytes: collections.Counter = collections.Counter()
        per_frames: collections.Counter = collections.Counter()
        per_drops: collections.Counter = collections.Counter()
        total_bytes = total_frames = total_drops = 0
        for _, sid, n, dropped in self._events:
            per_bytes[sid] += n
            per_frames[sid] += 1
            per_drops[sid] += dropped
            total_bytes += n
            total_frames += 1
            total_drops += dropped
        w = self._window
        out: list[tuple[Message, Channel]] = []
        video_bytes = 0
        for sid, topic in self._video_topics.items():
            b = per_bytes.get(sid, 0)
            video_bytes += b
            out.append(self._sample(
                _BITRATE_STREAM_BASE + sid, _BITRATE_TOPIC_PREFIX + topic, t,
                self._fields(b, per_frames.get(sid, 0), per_drops.get(sid, 0))))
        out.append(self._sample(
            _BITRATE_STREAM_BASE, _BITRATE_TOTAL_TOPIC, t,
            {**self._fields(total_bytes, total_frames, total_drops),
             "video_mbps": video_bytes * 8 / w / 1e6}))
        return out

    def _fields(self, nbytes: int, frames: int, drops: int) -> dict:
        w = self._window
        expected = frames + drops
        return {
            "mbps": nbytes * 8 / w / 1e6,
            "fps": frames / w,
            "bytes": nbytes,
            "drops": drops,
            "drop_pct": (100.0 * drops / expected) if expected else 0.0,
        }

    def _sample(self, stream_id: int, topic: str, t: int,
                fields: dict) -> tuple[Message, Channel]:
        ch = self._channels.get(stream_id)
        if ch is None:
            ch = Channel(id=stream_id, topic=topic, encoding="json",
                         schema_name=_BITRATE_SCHEMA_NAME, schema=_BITRATE_SCHEMA,
                         schema_encoding="jsonschema")
            self._channels[stream_id] = ch
        msg = Message(stream_id=stream_id,
                      payload=json.dumps(fields, separators=(",", ":")).encode(),
                      seq=self._seq)
        self._seq += 1
        msg.timestamp.FromNanoseconds(t)
        return msg, ch


# --------------------------------------------------------------------------- #
# Source                                                                       #
# --------------------------------------------------------------------------- #
def _read_fd_frames(fd: int, stop: threading.Event | None = None) -> Iterator[Message]:
    """Drive a non-blocking fd through the COBS de-framer until EOF or ``stop``,
    yielding decoded Messages. The byte path shared by the serial and TCP sources
    (they differ only in how the fd is opened): read with ``read_some`` under a
    selector and de-frame with ``extract_frames`` — the same COBS framing the bus
    and tests use, but inline (this tool is deliberately one read loop, no bus, no
    threads; the active-object endpoints own a thread, which we don't need here).
    The 0.2 s selector timeout bounds shutdown latency: an idle link re-checks
    ``stop`` at least that often. ``stop`` defaults to the module-global ``_STOP``
    (what the one-shot CLI + its SIGINT/SIGTERM handlers drive); ``--serve`` passes a
    per-bridge event to stop one device's reader without touching the global. Owns
    ``fd`` — closes it on exit."""
    stop = _STOP if stop is None else stop
    set_nonblocking(fd)
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    rx = bytearray()
    try:
        while not stop.is_set():
            if not sel.select(timeout=0.2):
                continue  # idle tick: re-check stop
            chunk = read_some(fd, 4096)
            if chunk is None:
                return  # EOF: link broke / device unplugged
            rx.extend(chunk)  # b"" on EAGAIN is harmless
            yield from extract_frames(rx)
    finally:
        sel.close()
        close_fd(fd)


def read_serial(port: str, baud: int, stop: threading.Event | None = None) -> Iterator[Message]:
    """Yield Messages from a live serial port. On POSIX, pyserial opens + configures
    the tty (baud, raw); we dup its fd so we own + close it independently of pyserial
    (termios settings live on the tty, so they persist on the dup after ``ser.close()``)
    and drive it through the shared :func:`_read_fd_frames`. Windows exposes no
    ``select()``-able serial fd, so there we keep the pyserial object and read through
    it (:func:`_read_serial_win`)."""
    import serial  # pyserial: opens + configures the tty (baud, raw)

    if sys.platform == "win32":
        yield from _read_serial_win(serial.Serial(port, baud, timeout=0.2), stop)
        return
    ser = serial.Serial(port, baud)
    fd = os.dup(ser.fileno())
    ser.close()
    yield from _read_fd_frames(fd, stop)


def _read_serial_win(ser, stop: threading.Event | None = None) -> Iterator[Message]:
    """Windows serial read loop. pyserial exposes no ``select()``-able fd on Windows,
    so read through the ``Serial`` object (blocking up to its 0.2 s timeout, which
    bounds ``stop`` latency) rather than the POSIX fd path. Owns + closes ``ser``."""
    stop = _STOP if stop is None else stop
    rx = bytearray()
    try:
        while not stop.is_set():
            chunk = ser.read(4096)   # whatever arrived within the timeout
            if chunk:
                rx.extend(chunk)
                yield from extract_frames(rx)
    finally:
        ser.close()


def _read_sock_win(sock, stop: threading.Event | None = None) -> Iterator[Message]:
    """Windows TCP read loop. ``os.read`` doesn't work on a Windows socket handle, so
    poll with ``selectors`` (which does support sockets on Windows) + ``recv`` instead
    of the POSIX fd path. Owns + closes ``sock``."""
    stop = _STOP if stop is None else stop
    sock.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ)
    rx = bytearray()
    try:
        while not stop.is_set():
            if not sel.select(timeout=0.2):
                continue
            try:
                chunk = sock.recv(4096)
            except BlockingIOError:
                continue
            except OSError:
                return
            if not chunk:
                return  # EOF: peer closed
            rx.extend(chunk)
            yield from extract_frames(rx)
    finally:
        sel.close()
        sock.close()


def read_tcp(host: str, port: int, stop: threading.Event | None = None) -> Iterator[Message]:
    """Yield Messages from a live TCP connection to a device's preview listener
    (``host:port``). The device runs a ``TcpAcceptor`` (transport/tcp.hpp) that
    speaks the same COBS-delimited core frames as the serial link, so the read
    path is identical — only the fd source differs. ``TCP_NODELAY`` +
    ``SO_KEEPALIVE`` mirror the C++ ``DialTcpFd`` dialer so small frames aren't
    Nagle-batched and a silently-dropped peer is eventually detected. ``detach()``
    hands the connected fd to the shared :func:`_read_fd_frames`, which owns it."""
    import socket

    sock = socket.create_connection((host, port), timeout=5.0)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if sys.platform == "win32":     # os.read() can't read a Windows socket handle
        yield from _read_sock_win(sock, stop)
        return
    yield from _read_fd_frames(sock.detach(), stop)


def read_serial_resolved(port: str, baud: int,
                         stop: threading.Event | None = None) -> Iterator[tuple[Message, Channel]]:
    """Live serial source as resolved (Message, Channel) pairs: a
    :class:`ChannelRegistry` learns DeviceInfo announces and resolves each data
    frame (drop-until-mapped) — the same routing the bus uses."""
    yield from ChannelRegistry().resolved(read_serial(port, baud, stop))


def read_tcp_resolved(host: str, port: int,
                      stop: threading.Event | None = None) -> Iterator[tuple[Message, Channel]]:
    """Live TCP source as resolved (Message, Channel) pairs. DeviceInfo announces
    are end-to-end forwarded over the bus's TCP leg too, so the same
    :class:`ChannelRegistry` resolution as serial applies."""
    yield from ChannelRegistry().resolved(read_tcp(host, port, stop))


def _parse_tcp(target: str) -> tuple[str, int]:
    """Parse a ``--tcp HOST[:PORT]`` target into ``(host, port)``; the port
    defaults to :data:`_DEFAULT_TCP_PORT` (the device's preview listener). IPv4
    addresses and (mDNS) hostnames only — no IPv6 literals, matching the rest of
    the stack."""
    host, sep, port = target.rpartition(":")
    if not sep:
        return target, _DEFAULT_TCP_PORT
    return host, int(port)


def _replay(path: str, stop: threading.Event | None = None) -> Iterator[tuple[Message, Channel]]:
    """Replay an MCAP via the canonical ``read_mcap``, stopping on SIGINT/SIGTERM."""
    stop = _STOP if stop is None else stop
    for pair in read_mcap(path):
        if stop.is_set():
            break
        yield pair


# --------------------------------------------------------------------------- #
# Sinks                                                                        #
# --------------------------------------------------------------------------- #
def _ns(ts: Timestamp) -> int:
    return ts.seconds * 1_000_000_000 + ts.nanos


class FoxgloveSink:
    """Publish Messages to a live Foxglove WebSocket server. Each stream_id
    becomes one protobuf channel built from the resolved Channel; Studio decodes
    payloads from the schema descriptor — we never parse them here."""

    def __init__(self, port: int) -> None:
        import foxglove

        self._fg = foxglove
        self._server = foxglove.start_server(port=port)
        self._channels: dict[int, object] = {}
        print(f"Foxglove WebSocket server on ws://localhost:{port}", file=sys.stderr)
        print(f"open Foxglove Studio at:\n  {self._server.app_url()}", file=sys.stderr)
        print(f"load the matching layout:\n"
              f"  Foxglove Studio ▸ Layouts ▸ Import from file ▸ {_LAYOUT_PATH}",
              file=sys.stderr)

    @property
    def port(self) -> int:
        """The port the WS server actually bound (resolves ``port=0`` auto-assign)."""
        return self._server.port

    def write(self, msg: Message, ch: Channel) -> None:
        channel = self._channels.get(msg.stream_id)
        if channel is None:
            channel = self._fg.Channel(
                ch.topic,
                message_encoding=ch.encoding or "protobuf",
                schema=self._fg.Schema(
                    name=ch.schema_name,
                    encoding=ch.schema_encoding or "protobuf",
                    data=ch.schema,
                ),
            )
            self._channels[msg.stream_id] = channel
        channel.log(msg.payload, log_time=_ns(msg.timestamp))

    def reset(self) -> None:
        """Drop the current device's channels so the next device starts clean —
        used by the ``--serve`` launcher on a device switch, where this one server
        outlives every source. The cached ``foxglove.Channel`` objects are keyed by
        wire ``stream_id``, which the next device reuses for *different* topics and
        schemas, so each must be closed and the table cleared (``write`` lazily
        recreates them for the new device). ``clear_session()`` then bumps the
        Foxglove session id so connected viewers reset their advertised topics."""
        for channel in self._channels.values():
            channel.close()
        self._channels.clear()
        self._server.clear_session()

    def close(self) -> None:
        self._server.stop()


class RerunSink:
    """Visualize Visio streams in a live Rerun viewer — a faithful port of
    an earlier Rerun renderer's rendering. Only the
    data model changes (UMI v3 MessagePack dict -> Visio protobuf / topic+schema);
    the Rerun calls (decode->Image, imu_world boxes, blueprint, timeline) are the
    reference's, which is the known-good behavior we're reproducing:

      * foxglove.CompressedVideo  -> H.265 decoded (PyAV) to rr.Image under cameras/*
      * ros geometry_msgs Quaternion -> a box rotated by the quat in the FLU
        imu_world 3D scene (static rr.Boxes3D + rr.Transform3D), + x/y/z/w scalars
      * sensor.v1.ImuRaw          -> latest sample's accel/gyro/mag/temp rr.Scalars
      * sensor.v1.SystemHealth    -> the set fields as rr.Scalars

    Decode runs on the read thread, exactly like the reference. The blueprint is
    re-sent only when a new stream appears (resending resets the 3D camera)."""

    # IMU orientation box + in-line slot layout (verbatim from the reference).
    _IMU_BOX_HALF = (0.04, 0.02, 0.005)
    _IMU_SLOT_SPACING = 2.0 * (_IMU_BOX_HALF[0] * 2)
    _IMU_COLORS: ClassVar[list[list[int]]] = [
        [100, 180, 255], [255, 100, 100], [100, 255, 100], [255, 255, 100],
        [255, 100, 255], [100, 255, 255], [255, 180, 100], [180, 100, 255],
        [100, 255, 180], [255, 100, 180], [180, 255, 100], [100, 180, 180],
        [200, 200, 100], [200, 100, 200], [100, 200, 200], [200, 200, 200],
    ]
    _AV_CODEC: ClassVar[dict[str, str]] = {
        "h265": "hevc", "hevc": "hevc", "h264": "h264", "avc": "h264", "av1": "av1",
    }

    def __init__(self, memory_limit: str = "2GB") -> None:
        import av
        import rerun as rr
        import rerun.blueprint as rrb

        # Same as the reference's setup_rerun(): cap viewer memory via the env
        # var, then init + spawn the viewer.
        os.environ.setdefault("RERUN_MEMORY_LIMIT", memory_limit)
        self._rr = rr
        self._rrb = rrb
        self._av = av
        rr.init("Visio", spawn=True)
        # IMU orientations live in their own FLU scene (+X fwd, +Y left, +Z up).
        rr.log("imu_world", rr.ViewCoordinates.FLU, static=True)
        self._decoders: dict[str, object] = {}    # entity -> av.CodecContext
        self._unsupported: set[str] = set()       # cameras with an unknown codec (warned once)
        self._imu_slot: dict[str, int] = {}        # imu base -> 3D slot index
        # Discovered streams driving the blueprint; re-sent only on change.
        self._cams: list[str] = []
        self._imu_bases: list[str] = []
        self._health_topic: str | None = None
        self._dirty = True

    def write(self, msg: Message, ch: Channel) -> None:
        # Sequence timeline keyed on the message timestamp — exactly the
        # reference's `rr.set_time("time_us", sequence=ts_us)`.
        self._rr.set_time("time_us", sequence=_ns(msg.timestamp) // 1000)
        name = ch.schema_name
        if name == _VIDEO_SCHEMA:
            self._log_video(msg, ch.topic)
        elif name == _QUAT_SCHEMA:
            self._log_orientation(msg, ch.topic)
        elif name == _IMU_RAW_SCHEMA:
            self._log_imu_raw(msg, ch.topic)
        elif name == _HEALTH_SCHEMA:
            self._log_health(msg, ch.topic)
        else:
            return  # control/derived streams (DeviceInfo, /tf, ...): nothing to draw
        if self._dirty:
            self._send_blueprint()

    def _log_video(self, msg: Message, topic: str) -> None:
        # Decode H.265 -> rr.Image, exactly like the reference's handle_video.
        rr, av = self._rr, self._av
        ent = "cameras/" + topic.lstrip("/")
        cv = CompressedVideo()
        cv.ParseFromString(msg.payload)
        dec = self._decoders.get(ent)
        if dec is None:
            codec = self._AV_CODEC.get(cv.format.lower())
            if codec is None:
                if ent not in self._unsupported:
                    self._unsupported.add(ent)
                    print(f"rerun: {ent}: unsupported video format {cv.format!r}",
                          file=sys.stderr)
                return
            dec = av.CodecContext.create(codec, "r")
            self._decoders[ent] = dec
            self._cams.append(topic)
            self._dirty = True
        try:
            for frame in dec.decode(av.Packet(cv.data)):
                img = frame.to_ndarray(format="rgb24")
                # compress() matters: raw 1080p RGB is ~6 MB/frame and would blow
                # the viewer's memory cap; JPEG keeps it ~tens of KB (as the reference).
                rr.log(ent, rr.Image(img, color_model="rgb"))
        except Exception as exc:  # partial NALs at startup heal at next IDR
            print(f"rerun: {ent} decode dropped: {exc}", file=sys.stderr)

    def _log_orientation(self, msg: Message, topic: str) -> None:
        # Mirror handle_imu's orientation: one box per IMU in imu_world, rotated
        # by the quat, laid out in a line by a stable slot index.
        rr = self._rr
        q = Quaternion()
        q.ParseFromString(msg.payload)
        base = topic.lstrip("/").rsplit("/", 1)[0]    # ".../imu/0/quat" -> ".../imu/0"
        ent = f"imu_world/{base}"
        if base not in self._imu_slot:
            slot = len(self._imu_slot)
            self._imu_slot[base] = slot
            self._imu_bases.append(base)
            self._dirty = True
            rr.log(ent, rr.Boxes3D(half_sizes=[list(self._IMU_BOX_HALF)],
                                   colors=[self._IMU_COLORS[slot % len(self._IMU_COLORS)]]),
                   static=True)
        rr.log(ent, rr.Transform3D(
            translation=[self._imu_slot[base] * self._IMU_SLOT_SPACING, 0.0, 0.0],
            rotation=rr.Quaternion(xyzw=[q.x, q.y, q.z, q.w])))
        rr.log(f"{base}/quat", rr.Scalars([q.x, q.y, q.z, q.w]))

    def _log_imu_raw(self, msg: Message, topic: str) -> None:
        raw = ImuRaw()
        raw.ParseFromString(msg.payload)
        if not raw.samples:
            return
        base = topic.lstrip("/")
        s = raw.samples[-1]   # latest sample of the bundle drives the live plots
        a, g = s.linear_acceleration, s.angular_velocity
        self._rr.log(f"{base}/accel", self._rr.Scalars([a.x, a.y, a.z]))
        self._rr.log(f"{base}/gyro", self._rr.Scalars([g.x, g.y, g.z]))
        if s.HasField("magnetic_field"):
            m = s.magnetic_field
            self._rr.log(f"{base}/mag", self._rr.Scalars([m.x, m.y, m.z]))
        if s.HasField("temperature_c"):
            self._rr.log(f"{base}/temperature", self._rr.Scalars(s.temperature_c))

    def _log_health(self, msg: Message, topic: str) -> None:
        base = topic.lstrip("/")
        if self._health_topic is None:
            self._health_topic = base
            self._dirty = True
        h = SystemHealth()
        h.ParseFromString(msg.payload)
        for field in ("cpu_temp_c", "cpu_usage_pct", "mem_free_bytes",
                      "disk_free_bytes", "stream_clients"):
            if h.HasField(field):
                self._rr.log(f"{base}/{field}",
                             self._rr.Scalars(float(getattr(h, field))))

    def _send_blueprint(self) -> None:
        # Called only when a new stream appeared (self._dirty). Resending resets
        # the 3D view's camera, so keep it to genuine structural changes.
        self._dirty = False
        rrb = self._rrb
        cam_views = [rrb.Spatial2DView(origin="cameras/" + t.lstrip("/"),
                                       name=t.lstrip("/"))
                     for t in self._cams]
        center = [*cam_views]
        if self._imu_bases:
            center.append(rrb.Spatial3DView(origin="imu_world", name="IMU orientation"))
        right = [rrb.TimeSeriesView(origin=b, name=b) for b in self._imu_bases]
        if self._health_topic:
            right.append(rrb.TimeSeriesView(origin=self._health_topic, name="system health"))
        columns = [rrb.Vertical(*center)] if center else []
        if right:
            columns.append(rrb.Vertical(*right))
        if not columns:
            return
        root = rrb.Horizontal(*columns, column_shares=[5, 3][:len(columns)]) \
            if len(columns) > 1 else columns[0]
        self._rr.send_blueprint(rrb.Blueprint(root, collapse_panels=True),
                                make_active=True)

    def close(self) -> None:
        pass  # the Rerun viewer is a separate process; nothing to flush/close.


# --------------------------------------------------------------------------- #
# Bridge core — shared by the one-shot CLI and the --serve launcher            #
# --------------------------------------------------------------------------- #
class Sink(Protocol):
    """Structural type for a display/record sink fanned to by :func:`run_bridge` —
    ``FoxgloveSink``, ``RerunSink``, ``McapWriter`` and the launcher's status sink all
    satisfy it (write a resolved pair; close on shutdown)."""

    def write(self, msg: Message, ch: Channel) -> None: ...
    def close(self) -> None: ...


def run_bridge(
    source: Iterator[tuple[Message, Channel]],
    sinks: list[Sink],
    *,
    derive_tf: bool = False,
    derive_bitrate: bool = False,
    bitrate_window: float = 2.0,
    close_sinks: bool = True,
) -> int:
    """Pump a resolved ``(Message, Channel)`` source into ``sinks`` and return the
    processed-message count. Each sink is any object with ``write(msg, ch)`` +
    ``close()`` (``FoxgloveSink``, ``RerunSink``, ``McapWriter`` all qualify), fanned
    to in order — so a caller that also records puts the ``McapWriter`` first.

    This is the loop the one-shot CLI (``main``) and the ``--serve`` launcher share.
    ``/tf`` and ``/stats/bitrate/*`` are derived only when asked. Stopping is the
    *source's* job: pass a ``stop`` event into ``read_*``/``_replay`` and its
    generator ends, ending this loop. ``close_sinks`` is ``True`` for the one-shot
    CLI (it owns its sinks) and ``False`` for the launcher, whose Foxglove server
    outlives any single device's source."""
    tf = TfDeriver() if derive_tf else None
    bitrate = BitrateDeriver(window=bitrate_window) if derive_bitrate else None
    n = 0

    def _fan_out(msg: Message, ch: Channel) -> None:
        for sink in sinks:
            sink.write(msg, ch)

    try:
        for msg, ch in source:
            _fan_out(msg, ch)
            derived = tf.derive(msg, ch) if tf else None
            if derived is not None:
                _fan_out(*derived)
            if bitrate is not None:
                for d_msg, d_ch in bitrate.feed(msg, ch):
                    _fan_out(d_msg, d_ch)
            n += 1
    finally:
        if close_sinks:
            for sink in sinks:
                sink.close()
    return n


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="visio-display", description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--serial", metavar="PORT",
                     help="live serial port to read from (e.g. /dev/ttyACM0)")
    src.add_argument("--tcp", metavar="HOST[:PORT]",
                     help="live TCP connection to a device's preview listener "
                          f"(port defaults to {_DEFAULT_TCP_PORT})")
    src.add_argument("--mcap-in", metavar="IN.mcap",
                     help="replay a recorded MCAP file (Rerun/MCAP sinks only)")
    src.add_argument("--serve", action="store_true",
                     help="run the device-picker launcher: discover connected devices "
                          "(serial / local AP / Wi-Fi), pick one in the browser, and open "
                          "it in Foxglove")
    p.add_argument("--baud", type=int, default=921600, help="serial baud (default 921600)")
    p.add_argument("--out", metavar="OUT.mcap", help="also record messages to an MCAP file")
    p.add_argument("--foxglove", action="store_true", help="serve live to Foxglove Studio")
    p.add_argument("--port", type=int, default=8765, help="Foxglove WS port (default 8765)")
    p.add_argument("--serve-port", type=int, default=8770,
                   help="--serve only: launcher web-UI port (default 8770)")
    p.add_argument("--viewer", choices=("desktop", "browser", "both"), default="both",
                   help="--serve only: which Foxglove to open on device select — the "
                        "desktop app (foxglove:// deep link, works offline), a browser tab "
                        "(app.foxglove.dev), or both (default both)")
    p.add_argument("--bitrate", action=argparse.BooleanOptionalAction, default=True,
                   help="publish realtime bitrate on /stats/bitrate/* whenever a "
                        "Foxglove or MCAP sink is active (default: on; --no-bitrate off)")
    p.add_argument("--bitrate-window", type=float, default=2.0, metavar="SECS",
                   help="bitrate sliding-window length in seconds (default 2.0)")
    p.add_argument("--rerun", action="store_true", help="display in the Rerun viewer")
    p.add_argument("--rerun-memory", metavar="LIMIT", default="2GB",
                   help="Rerun viewer memory cap; old data drops past it (default 2GB)")
    args = p.parse_args(argv)

    # --serve is a persistent launcher, not a one-shot pipe: it discovers devices
    # and starts/stops per-device bridges itself. Dispatch before the one-shot
    # source/sink wiring below. (serve.py is imported lazily so the codec-only /
    # one-shot paths never pull aiohttp + zeroconf.)
    if args.serve:
        from visio_schema.display import serve
        serve.run_serve(
            serve_port=args.serve_port,
            ws_port=args.port,
            viewer=args.viewer,
            baud=args.baud,
            bitrate=args.bitrate,
            bitrate_window=args.bitrate_window,
        )
        return 0

    # MCAP-file -> Foxglove is not a streaming job: Foxglove Studio opens MCAP
    # files natively and far better (seek/scrub). Refuse it and point the user
    # there rather than re-serving the file over a live WebSocket.
    if args.mcap_in and args.foxglove:
        print(f"warning: --foxglove does not apply to an MCAP file. Open it "
              f"directly in Foxglove Studio:\n"
              f"  File ▸ Open local file ▸ {args.mcap_in}\n"
              f"  (then Layouts ▸ Import from file ▸ {_LAYOUT_PATH})\n"
              f"Ignoring --foxglove.", file=sys.stderr)
        args.foxglove = False

    if not args.out and not args.foxglove and not args.rerun:
        p.error("choose at least one sink: --out, --foxglove, and/or --rerun")

    # Both signals stop the read loop so the `finally` finalizes the MCAP and
    # stops the WS server cleanly — Ctrl-C (SIGINT) on a terminal, SIGTERM from
    # a service manager / `kill` / `timeout`.
    signal.signal(signal.SIGINT, lambda *_: _STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: _STOP.set())

    recorder = McapWriter(args.out) if args.out else None
    display_sinks: list[Sink] = []
    if args.foxglove:
        display_sinks.append(FoxgloveSink(args.port))
    if args.rerun:
        display_sinks.append(RerunSink(memory_limit=args.rerun_memory))
    # Recorder first so it captures the raw fan-out order ahead of the display sinks.
    sinks = display_sinks if recorder is None else [recorder, *display_sinks]

    if args.mcap_in:
        source = _replay(args.mcap_in)
    elif args.tcp:
        host, tcp_port = _parse_tcp(args.tcp)
        source = read_tcp_resolved(host, tcp_port)
    else:
        source = read_serial_resolved(args.serial, args.baud)

    # The /tf transform lets Foxglove's 3D panel render IMU orientation, and the
    # json bitrate is only consumed by the Foxglove/MCAP sinks (Rerun renders
    # orientation directly and ignores the json) — so derive both only when a
    # Foxglove or MCAP sink is active, keeping the rerun-only path lean.
    want_derived = bool(args.foxglove or args.out)
    if args.bitrate and want_derived and args.foxglove:
        print(f"publishing realtime bitrate on {_BITRATE_TOTAL_TOPIC} "
              f"(+ per-camera {_BITRATE_TOPIC_PREFIX}/<topic>); plot the `mbps` field",
              file=sys.stderr)

    # Source stop is the module-global _STOP (driven by the SIGINT/SIGTERM handlers
    # above); run_bridge closes the sinks when the source ends.
    n = run_bridge(
        source, sinks,
        derive_tf=want_derived,
        derive_bitrate=bool(args.bitrate and want_derived),
        bitrate_window=args.bitrate_window,
    )
    print(f"done ({n} messages)", file=sys.stderr)
    return n


def run() -> None:
    """Console-script entry point (the ``visio-display`` command).

    Runs :func:`main` and exits 0 on a clean finish. :func:`main` returns the
    processed-message count for programmatic/test use, which is not a meaningful
    process exit code — so the installed command goes through this wrapper.
    """
    main()
