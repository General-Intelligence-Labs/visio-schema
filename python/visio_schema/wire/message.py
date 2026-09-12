"""Neutral in-memory Visio message — the wire Header fields + payload.

This is the codec-level view of a message: the `visio_schema.v1.wire.Header`
fields (a per-link `stream_id`, a `seq` counter, and a `timestamp`) plus the
inner payload bytes, with helpers to round-trip a Message through the core frame
codec. It carries no bus/transport semantics — higher layers (a separate
bus layer) own sequence stamping, stream-id remapping, and the heartbeat-beacon
`timestamp` rewrite.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from google.protobuf.message import Message as ProtoMessage
from google.protobuf.timestamp_pb2 import Timestamp

from visio_schema.v1.wire.header_pb2 import Header
from visio_schema.wire.codec.frame import decode_frame, encode_frame


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
    # This bulk frame is an H.265 sync point (VPS/SPS/PPS + IDR). Carried on the
    # wire because a hub relays video it did not produce and a recorder opens a
    # video channel only on a decodable keyframe — see the Header.keyframe
    # comment in proto/visio_schema/v1/wire/header.proto for what went wrong
    # while this lived only in memory.
    keyframe: bool = False

    def to_header(self) -> Header:
        """Build the wire Header protobuf for this Message."""
        h = Header()
        h.stream_id = self.stream_id
        h.seq = self.seq
        h.timestamp.CopyFrom(self.timestamp)
        h.keyframe = self.keyframe
        return h

    @classmethod
    def stamped(
        cls,
        payload: bytes | ProtoMessage,
        t_ns: int,
        *,
        seq: int = 0,
        stream_id: int = 0,
    ) -> Message:
        """Build a Message carrying ``payload`` at ``t_ns`` nanoseconds.

        The write-side mirror of `message_class`: that resolves a schema name to a
        class so you can *decode* a payload; this stamps a payload so you can
        *write* one. ``payload`` is either already-serialized bytes or a protobuf
        message (serialized here).

        ``t_ns`` is the payload's sensor **capture** time, matching the
        `timestamp` contract above — for derived data that is the source
        element's ``t_ns``, so the result merges into the source recording's
        timeline with no fixup.

        ``seq`` is caller-supplied and never inferred. On a relayed message the
        incoming ``seq`` is real data, and renumbering it would corrupt the
        per-stream counter a consumer uses to spot drops; a producer of derived
        data passes its own counter.

        Example:
            w.write(Message.stamped(pose.SerializeToString(), el.t_ns),
                    make_channel(topic, "foxglove.PoseInFrame"))
        """
        ts = Timestamp()
        ts.FromNanoseconds(int(t_ns))
        data = payload if isinstance(payload, bytes) else payload.SerializeToString()
        return cls(stream_id=stream_id, payload=data, seq=seq, timestamp=ts)

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
            keyframe=header.keyframe,
        )


def encode_message(msg: Message) -> bytes:
    """Serialize a Message into the core wire frame (no transport wrapper)."""
    return encode_frame(msg.to_header(), msg.payload)


def decode_message(frame: bytes) -> Message:
    """Parse a core wire frame into a Message. Raises FrameError on error."""
    header, payload = decode_frame(frame)
    return Message.from_header(header, payload)
