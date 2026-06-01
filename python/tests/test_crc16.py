"""CRC-16/CCITT-FALSE conformance per visio-schema/docs/framing.md §4."""
from __future__ import annotations

import pytest

from visio_schema.wire.codec.crc16 import crc16


def test_check_value() -> None:
    assert crc16(b"123456789") == 0x29B1


def test_empty() -> None:
    # CCITT-FALSE init 0xFFFF, no input -> 0xFFFF
    assert crc16(b"") == 0xFFFF


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"A", 0xB915),
        (b"AB", 0x4B74),
        (b"\x00", 0xE1F0),
        (b"\x00\x00\x00\x00", 0x84C0),
        (b"\xff" * 16, 0x6A4B),
        # All 256 byte values once — exercises every distinct input byte so a
        # seed/poll regression can't slip through on a narrow alphabet.
        (bytes(range(256)), 0x3FBD),
    ],
)
def test_known_values(data: bytes, expected: int) -> None:
    assert crc16(data) == expected
