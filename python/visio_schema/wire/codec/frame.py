"""Visio core frame codec per visio-schema/docs/protocol/framing.md §1.

Frame layout (always, regardless of transport):

    +------------+--------------+-----------+----------+
    | HEADER_LEN | header_pb    | payload   | CRC16    |
    | u8         | (N bytes)    | (M bytes) | u16_le   |
    +------------+--------------+-----------+----------+

HEADER_LEN is a single unsigned byte: the serialized Header is ~21-25
bytes and never approaches 255, and the Header evolves via optional
protobuf fields + the `visio_schema.wire.vN` package version — there is no
separate header version byte. CRC16 covers `HEADER_LEN || header_pb ||
payload`; the CRC bytes themselves are not covered.

Per-transport wrappers (TCP TOTAL_LEN prefix, COBS for serial, datagram
boundaries for UDP) are applied OUTSIDE this codec by the transport
implementation — see framing.md §3.
"""
from __future__ import annotations

import struct

from google.protobuf.message import DecodeError

from visio_schema.wire.codec.crc16 import crc16
from visio_schema.v1.wire.header_pb2 import Header


class FrameError(Exception):
    """Frame decode failed. Per framing.md §5, the reader logs and drops."""


def encode_frame(header: Header, payload: bytes) -> bytes:
    """Serialize a Header + payload into the core wire frame."""
    header_bytes = header.SerializeToString()
    header_len = len(header_bytes)
    if header_len > 0xFF:
        raise FrameError(f"Header too large for u8 HEADER_LEN: {header_len} bytes")
    prefix = struct.pack("<B", header_len)
    covered = prefix + header_bytes + payload
    crc = struct.pack("<H", crc16(covered))
    return covered + crc


def decode_frame(buf: bytes) -> tuple[Header, bytes]:
    """Parse a core wire frame. Raises FrameError on any shape/CRC error."""
    if len(buf) < 3:
        raise FrameError(f"Frame too short: {len(buf)} bytes (need >= 3)")

    (header_len,) = struct.unpack_from("<B", buf, 0)
    payload_end = 1 + header_len  # exclusive bound on header bytes
    if payload_end + 2 > len(buf):
        raise FrameError(f"HEADER_LEN={header_len} exceeds frame size {len(buf)}")

    covered = buf[: len(buf) - 2]
    (got_crc,) = struct.unpack_from("<H", buf, len(buf) - 2)
    want_crc = crc16(covered)
    if got_crc != want_crc:
        raise FrameError(f"CRC mismatch: got 0x{got_crc:04x}, want 0x{want_crc:04x}")

    header = Header()
    try:
        header.ParseFromString(buf[1:payload_end])
    except DecodeError as exc:
        raise FrameError(f"Header parse failed: {exc}") from exc

    payload = bytes(buf[payload_end : len(buf) - 2])
    return header, payload
