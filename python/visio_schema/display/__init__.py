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
ids are exactly the data-frame ids, so no remap is needed here. The shared
registry *absorbs* DeviceInfo once learned; this viewer additionally re-emits
each announce on the well-known `/device_info` topic so device identity/firmware
is recorded into the MCAP and visible in Foxglove (see
`_resolved_with_device_info`).

Deliberately minimal — one read loop, no bus, no threads. The heavier,
bus-integrated transport lives in a separate bus/transport layer.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import json
import os
import queue
import select
import selectors
import signal
import sys
import threading
import time
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
from visio_schema.foxglove.CompressedImage_pb2 import CompressedImage
from visio_schema.foxglove.CompressedVideo_pb2 import CompressedVideo
from visio_schema.foxglove.FrameTransform_pb2 import FrameTransform

# Low-level fd helpers (advanced/internal) — used to feed messages into resolved() as a generator.
from visio_schema.transport import close_fd, extract_frames, read_some, set_nonblocking
from visio_schema.v1.ros.geometry_msgs.quaternion_pb2 import Quaternion
from visio_schema.v1.sensor.imu_raw_pb2 import ImuRaw
from visio_schema.v1.sensor.system_health_pb2 import SystemHealth

# Control stream id for DeviceInfo announces — so this tool can surface them on
# the well-known /device_info channel (see _resolved_with_device_info).
from visio_schema.wire.control import DEVICE_INFO as _DEVICE_INFO

# Reconnect-tolerant registry for the relay-multiplex consumer (TCP :50002 viewer
# + foxglove bridge). A viewer-side policy, so it lives here in display/.
from visio_schema.display.relay_registry import RelayRegistry

# Payload schema names dispatched on (== the protobuf full names on the wire).
_QUAT_SCHEMA = "visio_schema.v1.ros.geometry_msgs.Quaternion"
_VIDEO_SCHEMA = "foxglove.CompressedVideo"
_IMAGE_SCHEMA = "foxglove.CompressedImage"
_IMU_RAW_SCHEMA = "visio_schema.v1.sensor.ImuRaw"
_HEALTH_SCHEMA = "visio_schema.v1.sensor.SystemHealth"
# CompressedVideo.format (lowercased) -> PyAV decoder name. Shared by RerunSink (renders
# to rr.Image) and VideoDecodeSink (transcodes to JPEG for HEVC-less browsers).
_AV_CODEC: dict[str, str] = {
    "h265": "hevc", "hevc": "hevc", "h264": "h264", "avc": "h264", "av1": "av1",
}
# Starter Foxglove layout shipped beside this script (panels mirror the Rerun view).
_LAYOUT_PATH = Path(__file__).resolve().parent / "ego_layout.json"
# Synthetic stream id for the derived /tf channel — outside the wire id space so
# it can't collide with an announced stream.
_TF_STREAM_ID = 0x7F000001
# Base for the host-transcoded JPEG channels (one per source video stream, at base + the source's
# stream_id). A reserved high range, distinct from _TF_STREAM_ID and the bitrate base, so it can't
# collide with an announced id.
_JPEG_STREAM_BASE = 0x7F020000
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
    # select.select, not selectors.DefaultSelector: on macOS the default selector is
    # kqueue, whose EVFILT_READ never fires for a pty/tty (a documented BSD limitation),
    # so a serial fd would look permanently idle and this loop would hang. select(2)
    # reports tty/pty readability on every platform. (This path is POSIX-only — the
    # Windows serial/TCP reads take the pyserial / _read_sock_win branches instead.)
    rx = bytearray()
    try:
        while not stop.is_set():
            r, _w, _x = select.select([fd], [], [], 0.2)
            if not r:
                continue  # idle tick: re-check stop
            chunk = read_some(fd, 4096)
            if chunk is None:
                return  # EOF: link broke / device unplugged
            rx.extend(chunk)  # b"" on EAGAIN is harmless
            yield from extract_frames(rx)
    finally:
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


def dial_tcp(host: str, port: int, *, timeout: float = 5.0):
    """Dial a device's TCP bus listener and return the connected socket (caller owns it —
    ``detach()`` the fd or ``close()`` it). ``TCP_NODELAY`` + ``SO_KEEPALIVE`` mirror the
    C++ ``DialTcpFd`` dialer so small frames aren't Nagle-batched and a silently-dropped
    peer is eventually detected. Shared by the read-only :func:`read_tcp` and the
    launcher's bidirectional endpoint."""
    import socket

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    return sock


def read_tcp(host: str, port: int, stop: threading.Event | None = None) -> Iterator[Message]:
    """Yield Messages from a live TCP connection to a device's preview listener
    (``host:port``). The device runs a ``TcpAcceptor`` (transport/tcp.hpp) that
    speaks the same COBS-delimited core frames as the serial link, so the read path is
    identical — only the fd source differs. ``detach()`` hands the connected fd to the
    shared :func:`_read_fd_frames`, which owns it."""
    sock = dial_tcp(host, port)
    if sys.platform == "win32":     # os.read() can't read a Windows socket handle
        yield from _read_sock_win(sock, stop)
        return
    yield from _read_fd_frames(sock.detach(), stop)


def _resolved_with_device_info(
    reg: ChannelRegistry, messages: Iterator[Message]
) -> Iterator[tuple[Message, Channel]]:
    """:meth:`ChannelRegistry.resolved`, but also surfacing each DeviceInfo
    announce on the well-known ``/device_info`` channel.

    The shared ``resolved()`` *absorbs* DeviceInfo: it learns the announced
    channels to build the ``stream_id -> Channel`` routing table and yields
    nothing (it's a control stream, not data — that's the routing contract).
    Right for a generic consumer, but it means this viewer's sinks never see
    device identity / firmware. visio-display wants that visible — recorded into
    the MCAP and shown in Foxglove on ``/device_info`` — so here, and only here,
    we re-emit each announce after :meth:`~ChannelRegistry.accept` has learned
    it. ``accept`` returns ``Routed(None, None)`` for an absorbed announce and a
    dropped-unmapped data frame alike, so the announce is identified by its
    control stream id. The registry's own behavior is unchanged."""
    for m in messages:
        message, channel = reg.accept(m)
        if channel is not None:
            yield message, channel
        elif m.stream_id == _DEVICE_INFO:
            # accept() learned + absorbed it; re-emit on the /device_info channel
            # (reg.resolve maps the control id to the well-known channel).
            yield m, reg.resolve(_DEVICE_INFO)


def read_serial_resolved(port: str, baud: int,
                         stop: threading.Event | None = None) -> Iterator[tuple[Message, Channel]]:
    """Live serial source as resolved (Message, Channel) pairs: a
    :class:`ChannelRegistry` learns DeviceInfo announces and resolves each data
    frame (drop-until-mapped) — the same routing the bus uses, plus DeviceInfo
    surfaced on /device_info for this viewer (see :func:`_resolved_with_device_info`)."""
    yield from _resolved_with_device_info(ChannelRegistry(), read_serial(port, baud, stop))


def read_tcp_resolved(host: str, port: int,
                      stop: threading.Event | None = None) -> Iterator[tuple[Message, Channel]]:
    """Live TCP source as resolved (Message, Channel) pairs. The TCP leg is a
    *relayed multiplex* (many devices on one link) with no per-device link-drop,
    so a :class:`RelayRegistry` is used instead of the strict single-source
    :class:`ChannelRegistry`: it adopts a reconnecting device's re-announced ids
    while still surfacing genuine same-topic collisions (see relay_registry).
    DeviceInfo announces are likewise surfaced on /device_info for this viewer
    (see :func:`_resolved_with_device_info`)."""
    yield from _resolved_with_device_info(RelayRegistry(), read_tcp(host, port, stop))


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
        # write() (reader thread) and reset() (a device switch) both touch _channels; a
        # bounded overlap is possible mid-switch, so guard it. See BridgeManager.
        self._lock = threading.Lock()
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
        with self._lock:
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
        with self._lock:
            for channel in self._channels.values():
                channel.close()
            self._channels.clear()
        self._server.clear_session()

    def close(self) -> None:
        self._server.stop()


# GPU decode backends to try, best first. The OS-native APIs (D3D11VA/DXVA2 on Windows,
# VideoToolbox on macOS) drive the GPU's HEVC decoder directly — NOT via Windows Media Foundation —
# so they need no MS Store 'HEVC Video Extensions'; the vendor backends (NVDEC/QSV/VAAPI/AMF) cover
# the rest. We try this whole ordered list intersected with what the ffmpeg build actually has,
# rather than a fixed per-OS guess (which missed, e.g., an NVIDIA box whose only backend is cuda).
_HWACCEL_PRIORITY = ("d3d11va", "dxva2", "videotoolbox", "cuda", "qsv", "vaapi", "drm", "amf")


def _hwaccel_device_types() -> tuple[str, ...]:
    """The GPU backends to attempt, best first — empty if ``VISIO_NO_HWACCEL`` is set (a safety
    valve for a flaky GPU driver + test determinism)."""
    return () if os.environ.get("VISIO_NO_HWACCEL") else _HWACCEL_PRIORITY


def _make_decoder(av, codec: str):
    """Return ``(decoder, hardware: bool)``. Try each available GPU backend (with software fallback
    enabled) and keep the first that actually engages hardware (``is_hwaccel``); otherwise fall back
    to a slice-threaded software decoder. Slice threading (not frame threading) parallelizes within
    a frame without the multi-frame output buffering a live view can't afford."""
    available = set(av.codec.hwaccel.hwdevices_available())
    for dt in _hwaccel_device_types():
        if dt not in available:
            continue
        try:
            hw = av.codec.hwaccel.HWAccel(device_type=dt, allow_software_fallback=True)
            dec = av.CodecContext.create(codec, "r", hwaccel=hw)
        except Exception:      # GPU device couldn't be created — try the next backend / software
            continue
        if dec.is_hwaccel:                     # same PyAV (12+) that has HWAccel has this
            return dec, True
    dec = av.CodecContext.create(codec, "r")
    dec.thread_count = 0
    dec.thread_type = "SLICE"
    return dec, False


class _VideoStream:
    """Per-source-stream transcode state: a persistent HEVC decoder (GPU or software) + a reused
    mjpeg encoder + the derived JPEG channel, plus real-time pacing bookkeeping (``base_*`` maps
    device time to wall time; ``skipping`` is keyframe-only catch-up mode)."""

    __slots__ = ("base_pts", "base_wall", "channel", "dec", "enc", "hw", "last_emit",
                 "reformat", "skipping")

    def __init__(self, dec, hw: bool) -> None:
        self.dec = dec
        self.hw = hw                          # decoding on the GPU?
        self.enc = None                       # mjpeg encoder (set by _open_stream)
        self.reformat = None                  # reused VideoReformatter (set by _open_stream)
        self.channel: Channel | None = None   # derived JPEG channel (set by _open_stream)
        self.base_wall: float | None = None   # wall clock at the pacing baseline
        self.base_pts = 0.0                   # device time at that baseline
        self.last_emit = 0.0                  # wall clock of the last frame we published
        self.skipping = False                 # decoding keyframes-only to catch up


class _VideoWorker(threading.Thread):
    """One decode+encode thread per source video stream, fed by the reader thread through a small
    bounded queue. Keeps the heavy codec work OFF the transport reader thread — which would
    otherwise starve the USB read and desync the framing (the ``COBS decode failed`` symptom) — and
    lets multiple cameras transcode in parallel. Under backpressure the queue drops its OLDEST
    frame: a live view prefers latency over completeness, and an H.265 reference gap self-heals at
    the next keyframe. A bad frame is dropped and can never kill the thread (or the bridge)."""

    def __init__(self, sink: VideoDecodeSink, sid: int, topic: str) -> None:
        super().__init__(name=f"visio-transcode-{sid & 0xFFFF}", daemon=True)
        self._sink = sink
        self._sid = sid
        self._topic = topic
        self._q: queue.Queue = queue.Queue(maxsize=sink._QUEUE)
        # NOT self._stop — that name shadows Thread._stop() and breaks join().
        self._stopped = threading.Event()
        self.hw = False                # set once the decoder opens (read by decode_mode)

    def submit(self, msg: Message) -> None:
        try:
            self._q.put_nowait(msg)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._q.get_nowait()   # drop the oldest queued frame to keep latency bounded
            with contextlib.suppress(queue.Full):
                self._q.put_nowait(msg)

    def stop(self) -> None:
        self._stopped.set()
        self.join(timeout=1.5)

    def run(self) -> None:
        sink, st, failing = self._sink, None, False
        while not self._stopped.is_set():
            try:
                msg = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                cv = CompressedVideo()
                cv.ParseFromString(msg.payload)
                if st is None:
                    st = sink._open_stream(self._sid, cv, self._topic)
                    self.hw = st.hw
                sink._process(st, cv, msg)
                failing = False
            except Exception as exc:
                if not failing:
                    failing = True
                    print(f"visio-display: {self._topic}: dropping video frames ({exc})",
                          file=sys.stderr)


class VideoDecodeSink:
    """Wraps a downstream sink so H.265 is viewable in browsers that can't decode HEVC. Decodes the
    device's H.265 (GPU if available, else software — see :func:`_make_decoder`) and re-encodes each
    frame to JPEG (``foxglove.CompressedImage``) on the SAME topic; the raw H.265 is dropped.
    Non-video messages pass straight through. The launcher inserts one in front of its
    ``FoxgloveSink`` only for sessions whose browser can't decode HEVC.

    The transport reader thread only ROUTES here: each video stream gets its own
    :class:`_VideoWorker` thread that owns the decoder+encoder, so the reader never blocks (no
    USB-read starvation) and cameras transcode in parallel. This is a **live** view, not a complete
    one — it keeps up with real time by dropping frames (rate cap ``_MIN_EMIT_INTERVAL``, plus
    keyframe-only catch-up once decode falls ``_MAX_LAG_S`` behind), never by playing a backlog in
    slow motion. (JPEG, not H.264: mjpeg is intra so it's low-latency and cheap to encode; a full
    H.264 re-encode was tried and dropped — openh264, the only ship-able encoder, does 1080p at
    ~2 fps.)"""

    _MIN_EMIT_INTERVAL = 1 / 15       # cap published video; drop the rest (bounds encode cost)
    _MAX_LAG_S = 0.5                   # behind real time by more than this → keyframe-only catch-up
    _RESYNC_LAG_S = 0.2               # …until back within this, then resume full decode
    _QUEUE = 8                         # per-stream frame backlog before dropping (gaps heal at IDR)

    def __init__(self, downstream: Sink) -> None:
        import av  # lazy: only this path needs ffmpeg, and it's excluded from lean bundles
        self._av = av
        self._down = downstream
        self._workers: dict[int, _VideoWorker] = {}   # source stream_id -> decode/encode thread
        self._raw_streams: set[int] = set()            # sids whose codec we can't decode
        self._lock = threading.Lock()

    # -- reader thread: route only (never decode here) --------------------- #
    def write(self, msg: Message, ch: Channel) -> None:
        if ch.schema_name != _VIDEO_SCHEMA:
            self._down.write(msg, ch)              # /tf, /stats/bitrate, control — untouched
            return
        sid = msg.stream_id
        if sid in self._raw_streams:
            self._down.write(msg, ch)              # a codec we can't decode → forward raw
            return
        w = self._workers.get(sid)
        if w is None:
            w = self._route_new(sid, msg, ch)
            if w is None:
                return                             # forwarded raw (unknown/unparseable codec)
        w.submit(msg)

    def _route_new(self, sid: int, msg: Message, ch: Channel) -> _VideoWorker | None:
        """First frame of a stream: if we can decode its codec, spawn a worker; otherwise forward
        the raw frame and remember to keep forwarding it (never re-parse)."""
        cv = CompressedVideo()
        try:
            cv.ParseFromString(msg.payload)
            decodable = cv.format.lower() in _AV_CODEC
        except Exception:
            decodable = False
        if not decodable:
            self._raw_streams.add(sid)
            print(f"visio-display: {ch.topic}: can't host-decode this video; forwarding as-is",
                  file=sys.stderr)
            self._down.write(msg, ch)
            return None
        with self._lock:
            w = self._workers.get(sid)
            if w is None:
                w = _VideoWorker(self, sid, ch.topic)
                self._workers[sid] = w
                w.start()
            return w

    # -- worker thread: decode + rate-cap + JPEG-encode -------------------- #
    def _open_stream(self, sid: int, cv: CompressedVideo, topic: str) -> _VideoStream:
        """Create the per-stream decoder + encoder + channel (on the worker thread — PyAV contexts
        are used from the one thread that owns them)."""
        dec, hw = _make_decoder(self._av, _AV_CODEC[cv.format.lower()])
        st = _VideoStream(dec=dec, hw=hw)
        st.enc = self._av.CodecContext.create("mjpeg", "w")
        st.reformat = self._av.video.reformatter.VideoReformatter()   # one sws context, reused
        st.channel = make_channel(topic, _IMAGE_SCHEMA, stream_id=_JPEG_STREAM_BASE + sid)
        print(f"visio-display: {topic}: transcoding "
              f"({'hardware' if hw else 'software'} decode → JPEG)", file=sys.stderr)
        return st

    def _process(self, st: _VideoStream, cv: CompressedVideo, msg: Message) -> None:
        now = time.monotonic()
        self._pace(st, now, _ns(cv.timestamp) / 1e9)
        for frame in st.dec.decode(self._av.Packet(cv.data)):
            # Drop decoded frames we're not due to show yet (rate cap) — but never drop the
            # scarce keyframes we're fast-forwarding to while catching up.
            if st.skipping or now - st.last_emit >= self._MIN_EMIT_INTERVAL:
                self._emit(st, cv, frame, msg)
                st.last_emit = now

    def _emit(self, st: _VideoStream, cv: CompressedVideo, frame, msg: Message) -> None:
        enc = st.enc
        if not enc.width:                          # mjpeg is intra + stateless; geometry set once
            enc.width, enc.height = frame.width, frame.height
            enc.pix_fmt = "yuvj420p"               # mjpeg wants full-range YUV
        yuv = st.reformat.reformat(frame, format="yuvj420p")
        data = b"".join(bytes(p) for p in enc.encode(yuv))
        ci = CompressedImage()
        ci.timestamp.CopyFrom(cv.timestamp)
        ci.frame_id = cv.frame_id
        ci.format = "jpeg"
        ci.data = data
        out = Message(stream_id=st.channel.id, payload=ci.SerializeToString(), seq=msg.seq)
        out.timestamp.CopyFrom(cv.timestamp)
        self._down.write(out, st.channel)

    def _pace(self, st: _VideoStream, now: float, pts: float) -> None:
        """Track how far decode has fallen behind real time and, when it can't keep up, tell the
        decoder to skip to the next keyframe — so the viewer sees LIVE video (dropped frames)
        rather than a growing backlog played in slow motion. ``lag`` is wall time elapsed minus
        device time elapsed since the baseline; it grows when we decode slower than real time and
        shrinks in keyframe-only mode (packets fly by), which is what pulls us back to live."""
        if st.base_wall is None:
            st.base_wall, st.base_pts = now, pts
        lag = (now - st.base_wall) - (pts - st.base_pts)
        if not st.skipping and lag > self._MAX_LAG_S:
            st.dec.skip_frame = "NONKEY"           # decode only keyframes → cheap fast-forward
            st.skipping = True
        elif st.skipping and lag < self._RESYNC_LAG_S:
            st.dec.skip_frame = "DEFAULT"
            st.skipping = False
            st.base_wall, st.base_pts = now, pts   # re-baseline once caught up

    def decode_mode(self) -> str | None:
        """``"hardware"`` if any stream is GPU-decoding, ``"software"`` if workers exist but none
        are, ``None`` before any video has arrived. Read by the launcher for its status."""
        with self._lock:
            workers = list(self._workers.values())
        if not workers:
            return None
        return "hardware" if any(w.hw for w in workers) else "software"

    def close(self) -> None:
        # Stop the per-stream worker threads (they hold PyAV decoders + write to the shared
        # FoxgloveSink) BEFORE the launcher resets/closes that sink on a device switch. The wrapped
        # FoxgloveSink itself is owned by BridgeManager — never close it here.
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.stop()


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
            codec = _AV_CODEC.get(cv.format.lower())
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
    p.add_argument("--port", type=int, default=8765,
                   help="--foxglove only: Foxglove WS port (default 8765). --serve always "
                        "auto-picks a free WS port.")
    p.add_argument("--serve-port", type=int, default=0,
                   help="--serve only: launcher web-UI port (default: auto-pick a free one)")
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
            ws_port=0,   # launcher wires the ws:// URL itself → always auto-pick a free port
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
