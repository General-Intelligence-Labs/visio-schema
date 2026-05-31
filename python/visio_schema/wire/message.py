"""Neutral in-memory Visio message — the wire Header fields + payload.

This is the codec-level view of a message: the `visio_schema.wire.v1.Header`
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
from visio_schema.wire.v1.header_pb2 import Header


@dataclass
class Message:
    """A wire message: `visio_schema.wire.v1.Header` fields + payload bytes."""

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
