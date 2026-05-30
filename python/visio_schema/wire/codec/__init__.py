"""Visio wire codecs — CRC, COBS, and the core frame.

Ships inside the `visio-schema` package alongside the generated protobuf
bindings: `from visio_schema.wire.codec import encode_frame` sits next to
`from visio_schema.wire.v1 import header_pb2`. These are the executable form of
the byte specs in visio-schema/docs/framing.md.
"""
from visio_schema.wire.codec.cobs import cobs_decode, cobs_encode
from visio_schema.wire.codec.crc16 import crc16
from visio_schema.wire.codec.frame import FrameError, decode_frame, encode_frame

__all__ = [
    "FrameError",
    "cobs_decode",
    "cobs_encode",
    "crc16",
    "decode_frame",
    "encode_frame",
]
