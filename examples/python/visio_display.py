#!/usr/bin/env python3
"""Display / record live Visio data: Foxglove, Rerun, and/or MCAP.

Reads Visio messages from a **live serial port** or a **live TCP connection**
(both COBS-delimited core frames, framing.md §3.2) **or replays a recorded MCAP
file**, and fans them out to any combination of a **live Foxglove Studio**
WebSocket server, a **live Rerun** viewer, and an **MCAP recording**. Depends
only on the `visio-schema` package plus a few thin libraries (see
requirements.txt).

    # live serial -> Rerun (spawns the viewer; auto-lays-out views)
    python visio_display.py --serial /dev/ttyACM0 --rerun

    # live TCP (the device's preview listener, default port 9000) -> Rerun
    python visio_display.py --tcp GILABS-1234.local --rerun

    # replay a recorded MCAP file into Rerun
    python visio_display.py --mcap-in run.mcap --rerun

    # live serial -> Foxglove Studio (prints a URL to open)
    python visio_display.py --serial /dev/ttyACM0 --foxglove

    # live TCP -> record an MCAP (and watch live at the same time)
    python visio_display.py --tcp 10.0.0.7:9000 --out run.mcap --rerun

The source is exactly one of `--serial` / `--tcp` / `--mcap-in`. MCAP-file ->
Foxglove is not supported (open the file directly in Foxglove Studio, which
seeks/scrubs); the script says so and ignores `--foxglove` for a file source.

`--foxglove` starts a WebSocket *data source* server (not itself a viewer) and
prints a URL; open it in Foxglove Studio, or in Studio choose
Open connection → Foxglove WebSocket → ws://localhost:8765. A starter layout is
in `ego_layout.json` (Studio ▸ Layouts ▸ Import from file).

`--rerun` spawns the Rerun viewer and drives an explicit layout (camera views,
a 3D IMU-orientation scene, IMU time-series). The Rerun rendering is a port of
`capstone/umi_data/scripts/display_stream_rerun.py` (H.265 decoded with PyAV to
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
bus-integrated transport lives in visio-mq.
"""
from __future__ import annotations

import argparse
import os
import selectors
import signal
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

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
from visio_schema.v1.ros.geometry_msgs.quaternion_pb2 import Quaternion
from visio_schema.v1.sensor.imu_raw_pb2 import ImuRaw
from visio_schema.v1.sensor.system_health_pb2 import SystemHealth

# Low-level fd helpers (advanced/internal) — used to feed messages into resolved() as a generator.
from visio_schema.transport import close_fd, extract_frames, read_some, set_nonblocking

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
# Source                                                                       #
# --------------------------------------------------------------------------- #
def _read_fd_frames(fd: int) -> Iterator[Message]:
    """Drive a non-blocking fd through the COBS de-framer until EOF or ``_STOP``,
    yielding decoded Messages. The byte path shared by the serial and TCP sources
    (they differ only in how the fd is opened): read with ``read_some`` under a
    selector and de-frame with ``extract_frames`` — the same COBS framing the bus
    and tests use, but inline (this tool is deliberately one read loop, no bus, no
    threads; the active-object endpoints own a thread, which we don't need here).
    The 0.2 s selector timeout bounds shutdown latency: an idle link re-checks
    ``_STOP`` at least that often. Owns ``fd`` — closes it on exit."""
    set_nonblocking(fd)
    sel = selectors.DefaultSelector()
    sel.register(fd, selectors.EVENT_READ)
    rx = bytearray()
    try:
        while not _STOP.is_set():
            if not sel.select(timeout=0.2):
                continue  # idle tick: re-check _STOP
            chunk = read_some(fd, 4096)
            if chunk is None:
                return  # EOF: link broke / device unplugged
            rx.extend(chunk)  # b"" on EAGAIN is harmless
            yield from extract_frames(rx)
    finally:
        sel.close()
        close_fd(fd)


def read_serial(port: str, baud: int) -> Iterator[Message]:
    """Yield Messages from a live serial port. pyserial opens + configures the tty
    (baud, raw); we then dup its fd so we own + close it independently of pyserial
    (termios settings live on the tty, so they persist on the dup after
    ``ser.close()``) and drive it through the shared :func:`_read_fd_frames`."""
    import serial  # pyserial: opens + configures the tty (baud, raw)

    ser = serial.Serial(port, baud)
    fd = os.dup(ser.fileno())
    ser.close()
    yield from _read_fd_frames(fd)


def read_tcp(host: str, port: int) -> Iterator[Message]:
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
    yield from _read_fd_frames(sock.detach())


def read_serial_resolved(port: str, baud: int) -> Iterator[tuple[Message, Channel]]:
    """Live serial source as resolved (Message, Channel) pairs: a
    :class:`ChannelRegistry` learns DeviceInfo announces and resolves each data
    frame (drop-until-mapped) — the same routing the bus uses."""
    yield from ChannelRegistry().resolved(read_serial(port, baud))


def read_tcp_resolved(host: str, port: int) -> Iterator[tuple[Message, Channel]]:
    """Live TCP source as resolved (Message, Channel) pairs. DeviceInfo announces
    are end-to-end forwarded over the bus's TCP leg too, so the same
    :class:`ChannelRegistry` resolution as serial applies."""
    yield from ChannelRegistry().resolved(read_tcp(host, port))


def _parse_tcp(target: str) -> tuple[str, int]:
    """Parse a ``--tcp HOST[:PORT]`` target into ``(host, port)``; the port
    defaults to :data:`_DEFAULT_TCP_PORT` (the device's preview listener). IPv4
    addresses and (mDNS) hostnames only — no IPv6 literals, matching the rest of
    the stack."""
    host, sep, port = target.rpartition(":")
    if not sep:
        return target, _DEFAULT_TCP_PORT
    return host, int(port)


def _replay(path: str) -> Iterator[tuple[Message, Channel]]:
    """Replay an MCAP via the canonical ``read_mcap``, stopping on SIGINT/SIGTERM."""
    for pair in read_mcap(path):
        if _STOP.is_set():
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

    def close(self) -> None:
        self._server.stop()


class RerunSink:
    """Visualize Visio streams in a live Rerun viewer — a faithful port of
    capstone/umi_data/scripts/display_stream_rerun.py's Rerun rendering. Only the
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
    _IMU_COLORS = [
        [100, 180, 255], [255, 100, 100], [100, 255, 100], [255, 255, 100],
        [255, 100, 255], [100, 255, 255], [255, 180, 100], [180, 100, 255],
        [100, 255, 180], [255, 100, 180], [180, 255, 100], [100, 180, 180],
        [200, 200, 100], [200, 100, 200], [100, 200, 200], [200, 200, 200],
    ]
    _AV_CODEC = {"h265": "hevc", "hevc": "hevc", "h264": "h264", "avc": "h264", "av1": "av1"}

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
        except Exception as exc:  # noqa: BLE001  partial NALs at startup heal at next IDR
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
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--serial", metavar="PORT",
                     help="live serial port to read from (e.g. /dev/ttyACM0)")
    src.add_argument("--tcp", metavar="HOST[:PORT]",
                     help="live TCP connection to a device's preview listener "
                          f"(port defaults to {_DEFAULT_TCP_PORT})")
    src.add_argument("--mcap-in", metavar="IN.mcap",
                     help="replay a recorded MCAP file (Rerun/MCAP sinks only)")
    p.add_argument("--baud", type=int, default=921600, help="serial baud (default 921600)")
    p.add_argument("--out", metavar="OUT.mcap", help="also record messages to an MCAP file")
    p.add_argument("--foxglove", action="store_true", help="serve live to Foxglove Studio")
    p.add_argument("--port", type=int, default=8765, help="Foxglove WS port (default 8765)")
    p.add_argument("--rerun", action="store_true", help="display in the Rerun viewer")
    p.add_argument("--rerun-memory", metavar="LIMIT", default="2GB",
                   help="Rerun viewer memory cap; old data drops past it (default 2GB)")
    args = p.parse_args(argv)

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
    sinks: list[FoxgloveSink | RerunSink] = []    # display sinks: write(msg, ch)
    if args.foxglove:
        sinks.append(FoxgloveSink(args.port))
    if args.rerun:
        sinks.append(RerunSink(memory_limit=args.rerun_memory))

    if args.mcap_in:
        source = _replay(args.mcap_in)
    elif args.tcp:
        host, tcp_port = _parse_tcp(args.tcp)
        source = read_tcp_resolved(host, tcp_port)
    else:
        source = read_serial_resolved(args.serial, args.baud)
    # The /tf transform exists so Foxglove's 3D panel can render IMU orientation;
    # Rerun renders it directly (rr.Transform3D), so only derive it when a
    # Foxglove or MCAP sink will use it — keeps the rerun-only path lean.
    tf = TfDeriver() if (args.foxglove or args.out) else None
    n = 0

    def _fan_out(msg: Message, ch: Channel) -> None:
        if recorder is not None:
            recorder.write(msg, ch)
        for sink in sinks:
            sink.write(msg, ch)

    try:
        for msg, ch in source:
            _fan_out(msg, ch)
            derived = tf.derive(msg, ch) if tf else None
            if derived is not None:
                _fan_out(*derived)
            n += 1
    finally:
        if recorder is not None:
            recorder.close()
        for sink in sinks:
            sink.close()
    print(f"done ({n} messages)", file=sys.stderr)
    return n


if __name__ == "__main__":
    raise SystemExit(main())
