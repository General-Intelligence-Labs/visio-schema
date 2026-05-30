#!/usr/bin/env python3
"""Minimal Visio → MCAP / Foxglove Studio bridge.

Read Visio messages from a **live serial port** or a **visio-written MCAP
file**, and send them to an **MCAP file** and/or a **live Foxglove Studio**
WebSocket server. Depends only on the `visio-schema` package plus three thin
libraries (see requirements.txt).

    # live serial -> Foxglove Studio (connect Studio to ws://localhost:8765)
    python visio_foxglove.py --serial /dev/ttyUSB0 --foxglove

    # live serial -> record an MCAP (and watch live at the same time)
    python visio_foxglove.py --serial /dev/ttyUSB0 --out run.mcap --foxglove

    # replay a recorded MCAP into Foxglove Studio
    python visio_foxglove.py --mcap run.mcap --foxglove

Foxglove Studio can also just open the written .mcap directly (File ▸ Open).

This is deliberately minimal — one blocking loop, no bus, no threads. The
heavier, bus-integrated recording/replay machinery lives in visio-mq.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator

from google.protobuf.timestamp_pb2 import Timestamp

from visio.wire.codec import FrameError, cobs_decode, decode_frame
from visio.wire.message import Message
from visio.wire.streams import (
    REGISTRY,
    file_descriptor_set,
    parse_topic,
    synthesized_topic,
)


# --------------------------------------------------------------------------- #
# Sources                                                                      #
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
            except (FrameError, Exception) as exc:  # noqa: BLE001 wire boundary
                print(f"drop: {exc}", file=sys.stderr)
                continue
            yield Message.from_header(header, payload)


def read_mcap(path: str) -> Iterator[Message]:
    """Yield Messages from a visio-written MCAP file."""
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        for _schema, channel, message in make_reader(f).iter_messages():
            try:
                device, stream, stream_index = parse_topic(channel.topic)
            except ValueError:
                continue  # not a synthesized visio topic; skip
            ts = Timestamp()
            ts.FromNanoseconds(message.log_time)
            yield Message(
                stream=stream,
                stream_index=stream_index,
                payload=message.data,
                device=device,
                seq=message.sequence,
                timestamp=ts,
            )


# --------------------------------------------------------------------------- #
# Sinks                                                                        #
# --------------------------------------------------------------------------- #
def _ns(ts: Timestamp) -> int:
    return ts.seconds * 1_000_000_000 + ts.nanos


class McapSink:
    """Write Messages to a spec-conformant MCAP (framing.md §5, MASTER_PLAN §5):
    payload bytes verbatim, schema from the protobuf FileDescriptorSet under
    its (possibly ROS-remapped) name, topic synthesized from the Header."""

    def __init__(self, path: str) -> None:
        from mcap.writer import Writer

        self._f = open(path, "wb")
        self._w = Writer(self._f)
        self._w.start()
        self._schema_ids: dict[str, int] = {}
        self._channel_ids: dict[str, int] = {}

    def write(self, msg: Message) -> None:
        mapping = REGISTRY.for_kind(msg.stream)
        if mapping is None:
            return  # unknown / unannotated stream; nothing to register
        schema_id = self._schema_ids.get(mapping.mcap_schema_name)
        if schema_id is None:
            schema_id = self._w.register_schema(
                name=mapping.mcap_schema_name,
                encoding="protobuf",
                data=file_descriptor_set(mapping.proto_type),
            )
            self._schema_ids[mapping.mcap_schema_name] = schema_id
        topic = synthesized_topic(msg.device, msg.stream, msg.stream_index)
        channel_id = self._channel_ids.get(topic)
        if channel_id is None:
            channel_id = self._w.register_channel(
                topic=topic, message_encoding="protobuf", schema_id=schema_id
            )
            self._channel_ids[topic] = channel_id
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
    """Publish Messages to a live Foxglove WebSocket server. Each (stream,
    stream_index) becomes one protobuf channel; Studio decodes payloads from
    the schema descriptor — we never parse them here."""

    def __init__(self, port: int) -> None:
        import foxglove

        self._fg = foxglove
        self._server = foxglove.start_server(port=port)
        self._channels: dict[str, object] = {}
        print(f"Foxglove WebSocket server on ws://localhost:{port}", file=sys.stderr)

    def write(self, msg: Message) -> None:
        mapping = REGISTRY.for_kind(msg.stream)
        if mapping is None:
            return
        topic = synthesized_topic(msg.device, msg.stream, msg.stream_index)
        channel = self._channels.get(topic)
        if channel is None:
            channel = self._fg.Channel(
                topic,
                message_encoding="protobuf",
                schema=self._fg.Schema(
                    name=mapping.mcap_schema_name,
                    encoding="protobuf",
                    data=file_descriptor_set(mapping.proto_type),
                ),
            )
            self._channels[topic] = channel
        channel.log(msg.payload, log_time=_ns(msg.timestamp))

    def close(self) -> None:
        self._server.stop()


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--serial", metavar="PORT", help="read live from a serial port")
    src.add_argument("--mcap", metavar="IN.mcap", help="read from an MCAP file")
    p.add_argument("--baud", type=int, default=921600, help="serial baud (default 921600)")
    p.add_argument("--out", metavar="OUT.mcap", help="also write messages to an MCAP file")
    p.add_argument("--foxglove", action="store_true", help="serve live to Foxglove Studio")
    p.add_argument("--port", type=int, default=8765, help="Foxglove WS port (default 8765)")
    args = p.parse_args(argv)

    if not args.out and not args.foxglove:
        p.error("choose at least one sink: --out and/or --foxglove")

    source = read_serial(args.serial, args.baud) if args.serial else read_mcap(args.mcap)
    sinks: list[McapSink | FoxgloveSink] = []
    if args.out:
        sinks.append(McapSink(args.out))
    if args.foxglove:
        sinks.append(FoxgloveSink(args.port))

    n = 0
    try:
        for msg in source:
            for sink in sinks:
                sink.write(msg)
            n += 1
        # A finite source (MCAP) drained: keep the Foxglove server up so
        # Studio can still connect and scrub the just-published messages.
        if args.foxglove:
            print(f"replayed {n} messages; serving — Ctrl-C to stop", file=sys.stderr)
            import time

            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for sink in sinks:
            sink.close()
    print(f"done ({n} messages)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
