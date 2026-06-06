"""Core frame encode/decode per visio-schema/docs/framing.md §1 (u8 HEADER_LEN)."""
from __future__ import annotations

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from visio_schema.wire.codec.frame import FrameError, decode_frame, encode_frame
from visio_schema.wire.message import Message, decode_message, encode_message
from visio_schema.v1.wire.header_pb2 import Header


def _make_header() -> Header:
    h = Header()
    h.stream_id = 17
    h.seq = 42
    ts = Timestamp()
    ts.seconds = 1_700_000_000
    ts.nanos = 123_456_789
    h.timestamp.CopyFrom(ts)
    return h


def test_roundtrip_simple() -> None:
    header = _make_header()
    payload = b"\xde\xad\xbe\xef"
    frame = encode_frame(header, payload)
    decoded_header, decoded_payload = decode_frame(frame)
    assert decoded_header == header
    assert decoded_payload == payload


def test_roundtrip_empty_payload() -> None:
    header = _make_header()
    frame = encode_frame(header, b"")
    decoded_header, decoded_payload = decode_frame(frame)
    assert decoded_header == header
    assert decoded_payload == b""


def test_roundtrip_large_payload() -> None:
    header = _make_header()
    payload = bytes(range(256)) * 16  # 4 KiB
    frame = encode_frame(header, payload)
    decoded_header, decoded_payload = decode_frame(frame)
    assert decoded_header == header
    assert decoded_payload == payload


def test_message_roundtrip() -> None:
    msg = Message(
        stream_id=17,
        payload=b"\x01\x02\x03",
        seq=7,
    )
    msg.timestamp.seconds = 1_700_000_000
    decoded = decode_message(encode_message(msg))
    assert decoded == msg


def test_corrupt_crc_raises() -> None:
    frame = bytearray(encode_frame(_make_header(), b"hello"))
    frame[-1] ^= 0xFF
    with pytest.raises(FrameError, match="CRC mismatch"):
        decode_frame(bytes(frame))


def test_corrupt_header_byte_raises() -> None:
    # Flip a byte inside the header — both the header parse AND the CRC
    # will fail. Either error is acceptable; we just need it to raise.
    frame = bytearray(encode_frame(_make_header(), b"hello"))
    frame[3] ^= 0xFF
    with pytest.raises(FrameError):
        decode_frame(bytes(frame))


def test_header_len_overflow_raises() -> None:
    # Craft a frame whose declared HEADER_LEN exceeds the buffer.
    buf = bytes([200]) + b"\x00" * 10
    with pytest.raises(FrameError, match="HEADER_LEN"):
        decode_frame(buf)


def test_short_frame_raises() -> None:
    with pytest.raises(FrameError, match="too short"):
        decode_frame(b"\x00\x00")


class _OversizeHeader:
    """Duck-typed Header whose serialization exceeds the u8 HEADER_LEN cap."""

    def SerializeToString(self) -> bytes:
        return b"\x00" * 256


def test_header_too_large_for_u8_raises() -> None:
    with pytest.raises(FrameError, match="HEADER_LEN"):
        encode_frame(_OversizeHeader(), b"")
