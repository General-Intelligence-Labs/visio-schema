"""Neutral in-memory Visio message — the wire Header fields + payload.

This is the codec-level view of a message: the `visio_schema.v1.wire.Header`
fields (a per-link `stream_id`, a `seq` counter, and a `timestamp`) plus the
inner payload bytes, with helpers to round-trip a Message through the core frame
codec. It carries no bus/transport semantics — higher layers (e.g. visio-mq's
Bus) own sequence stamping, stream-id remapping, and the heartbeat-beacon
`timestamp` rewrite.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from google.protobuf.timestamp_pb2 import Timestamp

from visio_schema.wire.codec.frame import decode_frame, encode_frame
from visio_schema.v1.wire.header_pb2 import Header


@dataclass
class Message:
    """One Visio wire message: the header fields plus the payload bytes.

    The codec-level view of a message — what `read_serial` / `read_mcap` yield and
    what `Endpoint.send` / `McapWriter.write` accept. The payload is the
    already-serialized protobuf for the stream's type; decode it with
    ``message_class(channel.schema_name)``.

    Attributes:
        stream_id: The per-link stream this message belongs to — a control id (e.g.
            `COMMAND`), or a data id that `ChannelRegistry` resolves to a `Channel`.
        payload: The serialized protobuf payload bytes.
        seq: Per-stream sequence counter (uint32).
        timestamp: The payload's sensor **capture** time (NOT send/publish time),
            a ``google.protobuf.Timestamp`` — the producer contract in
            ``docs/protocol/timesync.md``. A relay re-expresses it into its own
            clock via the heartbeat offset; control/transport messages with no
            sensor instant carry the send time instead.

    Example:
        msg = Message(stream_id=16, payload=imu.SerializeToString())
        # decode a received payload:
        imu = message_class("visio_schema.v1.sensor.ImuRaw")()
        imu.ParseFromString(msg.payload)
    """

    stream_id: int = 0                               # per-link stream label
    payload: bytes = b""

    seq: int = 0                                     # uint32, per stream_id
    timestamp: Timestamp = field(default_factory=Timestamp)

    def to_header(self) -> Header:
        """Build the wire Header protobuf for this Message."""
        h = Header()
        h.stream_id = self.stream_id
        h.seq = self.seq
        h.timestamp.CopyFrom(self.timestamp)
        return h

    @classmethod
    def from_header(cls, header: Header, payload: bytes) -> Message:
        """Build a Message from a decoded Header + payload bytes."""
        ts = Timestamp()
        ts.CopyFrom(header.timestamp)
        return cls(
            stream_id=header.stream_id,
            payload=payload,
            seq=header.seq,
            timestamp=ts,
        )


def encode_message(msg: Message) -> bytes:
    """Serialize a Message into the core wire frame (no transport wrapper)."""
    return encode_frame(msg.to_header(), msg.payload)


def decode_message(frame: bytes) -> Message:
    """Parse a core wire frame into a Message. Raises FrameError on error."""
    header, payload = decode_frame(frame)
    return Message.from_header(header, payload)
