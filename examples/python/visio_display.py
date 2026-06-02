#!/usr/bin/env python3
"""Display / record live Visio serial data: Foxglove, Rerun, and/or MCAP.

Reads Visio messages from a **live serial port** (COBS-delimited core frames,
framing.md §3.2) **or replays a recorded MCAP file**, and fans them out to any
combination of a **live Foxglove Studio** WebSocket server, a **live Rerun**
viewer, and an **MCAP recording**. Depends only on the `visio-schema` package
plus a few thin libraries (see requirements.txt).

    # live serial -> Rerun (spawns the viewer; auto-lays-out views)
    python visio_display.py --serial /dev/ttyACM0 --rerun

    # replay a recorded MCAP file into Rerun
    python visio_display.py --mcap-in run.mcap --rerun

    # live serial -> Foxglove Studio (prints a URL to open)
    python visio_display.py --serial /dev/ttyACM0 --foxglove

    # live serial -> record an MCAP (and watch live at the same time)
    python visio_display.py --serial /dev/ttyACM0 --out run.mcap --rerun

The source is exactly one of `--serial` / `--mcap-in`. MCAP-file -> Foxglove is
not supported (open the file directly in Foxglove Studio, which seeks/scrubs);
the script says so and ignores `--foxglove` for a file source.

`--foxglove` starts a WebSocket *data source* server (not itself a viewer) and
prints a URL; open it in Foxglove Studio, or in Studio choose
Open connection → Foxglove WebSocket → ws://localhost:8765. A starter layout is
in `visio_layout.json` (Studio ▸ Layouts ▸ Import from file).

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
import signal
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from google.protobuf.timestamp_pb2 import Timestamp
from visio_schema.foxglove.CompressedVideo_pb2 import CompressedVideo
from visio_schema.foxglove.FrameTransform_pb2 import FrameTransform
from visio_schema.ros.geometry_msgs.v1.quaternion_pb2 import Quaternion
from visio_schema.sensor.v1.imu_raw_pb2 import ImuRaw
from visio_schema.sensor.v1.system_health_pb2 import SystemHealth
from visio_schema.service.device_info.v1.device_info_pb2 import Channel, DeviceInfo

from visio_schema.wire.codec import cobs_decode, decode_frame
from visio_schema.wire.message import Message
from visio_schema.wire.streams import file_descriptor_set
from visio_schema.wire.v1.header_pb2 import ControlStream

_DEVICE_INFO = ControlStream.CONTROL_STREAM_DEVICE_INFO
# Payload schema names dispatched on (== the protobuf full names on the wire).
_QUAT_SCHEMA = "visio_schema.ros.geometry_msgs.v1.Quaternion"
_VIDEO_SCHEMA = "foxglove.CompressedVideo"
_IMU_RAW_SCHEMA = "visio_schema.sensor.v1.ImuRaw"
_HEALTH_SCHEMA = "visio_schema.sensor.v1.SystemHealth"
# Starter Foxglove layout shipped beside this script (panels mirror the Rerun view).
_LAYOUT_PATH = Path(__file__).resolve().parent / "visio_layout.json"
# Synthetic stream id for the derived /tf channel — outside the wire id space so
# it can't collide with an announced stream.
_TF_STREAM_ID = 0x7F000001

# Set by SIGINT/SIGTERM so the read loop exits and the `finally` finalizes the
# MCAP. Handling SIGTERM (not just Ctrl-C) matters when this runs under a
# service manager or `timeout`/`kill` — otherwise the MCAP is left unfinalized.
_STOP = threading.Event()


# --------------------------------------------------------------------------- #
# Channel table: learn stream_id -> Channel from DeviceInfo announces          #
# --------------------------------------------------------------------------- #
class ChannelTable:
    """Maps a data-frame ``stream_id`` to the announced :class:`Channel`
    describing it (topic + schema). Fed by DeviceInfo announces; over a direct
    link the announced ``Channel.id`` equals the data-frame ``stream_id``."""

    def __init__(self) -> None:
        self._by_id: dict[int, Channel] = {}

    def learn(self, di: DeviceInfo) -> None:
        for ch in di.channels:
            self._by_id[ch.id] = ch

    def resolve(self, stream_id: int) -> Channel | None:
        return self._by_id.get(stream_id)


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
        self._channel = Channel(
            id=_TF_STREAM_ID, topic="/tf", encoding="protobuf",
            schema_name="foxglove.FrameTransform",
            schema=file_descriptor_set("foxglove.FrameTransform"),
            schema_encoding="protobuf",
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
def read_serial(port: str, baud: int) -> Iterator[Message]:
    """Yield Messages from a live serial port (COBS-delimited core frames,
    framing.md §3.2). Malformed frames are logged and skipped."""
    import serial  # pyserial

    # The 0.2 s read timeout doubles as the shutdown latency: ser.read returns
    # at least that often, so the loop re-checks _STOP even on an idle link.
    ser = serial.Serial(port, baud, timeout=0.2)
    buf = bytearray()
    try:
        while not _STOP.is_set():
            chunk = ser.read(4096)
            if chunk:
                buf.extend(chunk)
            while True:
                delim = buf.find(b"\x00")
                if delim < 0:
                    break
                encoded = bytes(buf[:delim])
                del buf[: delim + 1]
                if not encoded:
                    continue
                try:
                    header, payload = decode_frame(cobs_decode(encoded))
                except Exception as exc:  # noqa: BLE001 drop malformed frame, framing.md §5
                    print(f"drop: {exc}", file=sys.stderr)
                    continue
                yield Message.from_header(header, payload)
    finally:
        ser.close()


def read_serial_resolved(port: str, baud: int) -> Iterator[tuple[Message, Channel]]:
    """Live serial source as (Message, Channel) pairs: learn the stream_id ->
    Channel map from DeviceInfo announces and resolve each data frame against it
    (drop-until-mapped)."""
    table = ChannelTable()
    for msg in read_serial(port, baud):
        if msg.stream_id == _DEVICE_INFO:
            di = DeviceInfo()
            di.ParseFromString(msg.payload)
            table.learn(di)
            continue
        ch = table.resolve(msg.stream_id)
        if ch is not None:  # else drop-until-mapped: announce not seen yet
            yield msg, ch


def read_mcap(path: str) -> Iterator[tuple[Message, Channel]]:
    """Replay an MCAP recording as (Message, Channel) pairs. Each MCAP channel is
    self-describing (topic + schema record), so no DeviceInfo is needed — the
    Channel is rebuilt straight from the file. Timestamps come from the MCAP
    log_time, so a downstream Rerun sink replays them on the right timeline."""
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        for schema, channel, message in make_reader(f).iter_messages():
            if _STOP.is_set():
                break
            if schema is None:
                # No schema record -> the payload type is unresolvable; a sink
                # would silently fail to render it. Surface it instead.
                print(f"skip: MCAP channel {channel.topic!r} has no schema",
                      file=sys.stderr)
                continue
            ch = Channel(
                id=channel.id, topic=channel.topic,
                encoding=channel.message_encoding or "protobuf",
                schema_name=schema.name, schema=schema.data,
                schema_encoding=schema.encoding or "protobuf",
            )
            msg = Message(stream_id=channel.id, payload=message.data, seq=message.sequence)
            msg.timestamp.FromNanoseconds(message.log_time)
            yield msg, ch


# --------------------------------------------------------------------------- #
# Sinks                                                                        #
# --------------------------------------------------------------------------- #
def _ns(ts: Timestamp) -> int:
    return ts.seconds * 1_000_000_000 + ts.nanos


class McapSink:
    """Write Messages to a spec-conformant MCAP: payload bytes verbatim, schema
    and topic taken from the resolved :class:`Channel` (the DeviceInfo announce).

    The schema NAME is the protobuf full name (Channel.schema_name) and the
    schema DATA is the FileDescriptorSet (Channel.schema): for a protobuf
    channel Foxglove resolves the type by looking the schema name up inside the
    embedded set, so they must match. A message whose stream_id has not been
    announced yet is dropped (drop-until-mapped)."""

    def __init__(self, path: str) -> None:
        from mcap.writer import Writer

        self._f = open(path, "wb")
        self._w = Writer(self._f)
        self._w.start()
        self._schema_ids: dict[str, int] = {}
        self._channel_ids: dict[int, int] = {}

    def write(self, msg: Message, ch: Channel) -> None:
        schema_id = self._schema_ids.get(ch.schema_name)
        if schema_id is None:
            schema_id = self._w.register_schema(
                name=ch.schema_name,
                encoding=ch.schema_encoding or "protobuf",
                data=ch.schema,
            )
            self._schema_ids[ch.schema_name] = schema_id

        channel_id = self._channel_ids.get(msg.stream_id)
        if channel_id is None:
            channel_id = self._w.register_channel(
                topic=ch.topic,
                message_encoding=ch.encoding or "protobuf",
                schema_id=schema_id,
            )
            self._channel_ids[msg.stream_id] = channel_id

        ts = _ns(msg.timestamp)
        self._w.add_message(
            channel_id=channel_id,
            log_time=ts,
            data=msg.payload,
            publish_time=ts,
            sequence=msg.seq,
        )

    def close(self) -> None:
        self._w.finish()
        self._f.close()


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
        import os

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

    sinks: list[McapSink | FoxgloveSink | RerunSink] = []
    if args.out:
        sinks.append(McapSink(args.out))
    if args.foxglove:
        sinks.append(FoxgloveSink(args.port))
    if args.rerun:
        sinks.append(RerunSink(memory_limit=args.rerun_memory))

    source = read_mcap(args.mcap_in) if args.mcap_in \
        else read_serial_resolved(args.serial, args.baud)
    # The /tf transform exists so Foxglove's 3D panel can render IMU orientation;
    # Rerun renders it directly (rr.Transform3D), so only derive it when a
    # Foxglove or MCAP sink will use it — keeps the rerun-only path lean.
    tf = TfDeriver() if (args.foxglove or args.out) else None
    n = 0
    try:
        for msg, ch in source:
            for sink in sinks:
                sink.write(msg, ch)
            derived = tf.derive(msg, ch) if tf else None
            if derived is not None:
                for sink in sinks:
                    sink.write(*derived)
            n += 1
    finally:
        for sink in sinks:
            sink.close()
    print(f"done ({n} messages)", file=sys.stderr)
    return n


if __name__ == "__main__":
    raise SystemExit(main())
