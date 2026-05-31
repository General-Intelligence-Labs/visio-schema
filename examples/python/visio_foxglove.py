#!/usr/bin/env python3
"""Stream live Visio serial data to Foxglove Studio and/or an MCAP file.

Reads Visio messages from a **live serial port** (COBS-delimited core frames,
framing.md §3.2) and fans them out to a **live Foxglove Studio** WebSocket
server and/or an **MCAP recording**. Depends only on the `visio-schema`
package plus a couple of thin libraries (see requirements.txt).

    # live serial -> Foxglove Studio (it prints a URL to open)
    python visio_foxglove.py --serial /dev/ttyUSB0 --foxglove

    # live serial -> record an MCAP (and watch live at the same time)
    python visio_foxglove.py --serial /dev/ttyUSB0 --out run.mcap --foxglove

`--foxglove` starts a WebSocket *data source* server (not itself a viewer) and
prints a URL; open it in Foxglove Studio, or in Studio choose
Open connection → Foxglove WebSocket → ws://localhost:8765.

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
import sys
from collections.abc import Iterator

from google.protobuf.timestamp_pb2 import Timestamp
from visio_schema.service.device_info.v1.device_info_pb2 import Channel, DeviceInfo

from visio_schema.wire.codec import cobs_decode, decode_frame
from visio_schema.wire.message import Message
from visio_schema.wire.v1.header_pb2 import ControlStream

_DEVICE_INFO = ControlStream.CONTROL_STREAM_DEVICE_INFO


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
# Source                                                                       #
# --------------------------------------------------------------------------- #
def read_serial(port: str, baud: int) -> Iterator[Message]:
    """Yield Messages from a live serial port (COBS-delimited core frames,
    framing.md §3.2). Malformed frames are logged and skipped."""
    import serial  # pyserial

    ser = serial.Serial(port, baud, timeout=0.2)
    buf = bytearray()
    while True:
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


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--serial", metavar="PORT", required=True,
                   help="serial port to read live from (e.g. /dev/ttyUSB0)")
    p.add_argument("--baud", type=int, default=921600, help="serial baud (default 921600)")
    p.add_argument("--out", metavar="OUT.mcap", help="also record messages to an MCAP file")
    p.add_argument("--foxglove", action="store_true", help="serve live to Foxglove Studio")
    p.add_argument("--port", type=int, default=8765, help="Foxglove WS port (default 8765)")
    args = p.parse_args(argv)

    if not args.out and not args.foxglove:
        p.error("choose at least one sink: --out and/or --foxglove")

    sinks: list[McapSink | FoxgloveSink] = []
    if args.out:
        sinks.append(McapSink(args.out))
    if args.foxglove:
        sinks.append(FoxgloveSink(args.port))

    table = ChannelTable()
    n = 0
    try:
        for msg in read_serial(args.serial, args.baud):
            if msg.stream_id == _DEVICE_INFO:
                di = DeviceInfo()
                di.ParseFromString(msg.payload)
                table.learn(di)
                continue
            ch = table.resolve(msg.stream_id)
            if ch is None:
                continue  # drop-until-mapped: announce not seen yet
            for sink in sinks:
                sink.write(msg, ch)
            n += 1
    except KeyboardInterrupt:
        pass
    finally:
        for sink in sinks:
            sink.close()
    print(f"done ({n} messages)", file=sys.stderr)
    return n


if __name__ == "__main__":
    raise SystemExit(main())
