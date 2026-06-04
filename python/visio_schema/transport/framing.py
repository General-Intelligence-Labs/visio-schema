"""COBS-delimited core-frame de/framing — the single Python implementation.

Serial/byte transports carry COBS-delimited core frames (framing.md §3.2). This
is the one place the split-on-0x00 + COBS-decode + frame-decode loop lives;
:class:`SerialEndpoint`, the byte-stream reader (:func:`read_frames`), and the
examples all use it instead of hand-rolling it.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator

from visio_schema.wire.codec import cobs_decode, cobs_encode, decode_frame, encode_frame
from visio_schema.wire.message import Message

log = logging.getLogger(__name__)


def frame_bytes(msg: Message) -> bytes:
    """Serialize a Message to its on-the-wire COBS frame (with the 0x00
    delimiter): ``COBS(HEADER_LEN || header || payload || CRC) || 0x00``."""
    return cobs_encode(encode_frame(msg.to_header(), msg.payload)) + b"\x00"


def extract_frames(rx_buf: bytearray) -> list[Message]:
    """Pull every complete 0x00-delimited frame out of ``rx_buf`` (consuming the
    bytes it processes, leaving any partial trailing frame) and return the
    decoded Messages. Malformed frames are logged and skipped (framing.md §5)."""
    msgs: list[Message] = []
    # Advance a cursor and delete consumed bytes ONCE at the end, rather than an
    # O(remaining) ``del rx_buf[:n]`` shift per frame.
    pos = 0
    while True:
        delim = rx_buf.find(b"\x00", pos)
        if delim < 0:
            break
        encoded = bytes(rx_buf[pos:delim])
        pos = delim + 1
        if not encoded:
            continue
        try:
            header, payload = decode_frame(cobs_decode(encoded))
        except Exception as exc:
            log.warning("dropping malformed frame: %s", exc)
            continue
        msgs.append(Message.from_header(header, payload))
    if pos:
        del rx_buf[:pos]
    return msgs


def read_frames(chunks: Iterable[bytes]) -> Iterator[Message]:
    """Yield Messages from an iterable of byte chunks (e.g. successive serial
    reads), buffering partial frames across chunk boundaries. The byte-stream
    convenience over :func:`extract_frames`; transport (opening the port) stays
    with the caller."""
    buf = bytearray()
    for chunk in chunks:
        if chunk:
            buf.extend(chunk)
        yield from extract_frames(buf)

