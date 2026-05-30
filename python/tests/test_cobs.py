"""COBS encode/decode round-trips per visio-schema/docs/framing.md §3.2."""
from __future__ import annotations

import pytest

from visio_schema.wire.codec.cobs import cobs_decode, cobs_encode


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x00",
        b"\x00" * 32,
        b"\xff",
        b"\xff" * 254,                 # exactly the COBS run-length boundary
        b"\xff" * 255,                 # one past the boundary
        b"abc",
        b"\x00\x01\x02\x03\x00\x04",
        bytes(range(256)),
        b"\xff" * 300,                 # multi-block payload
    ],
)
def test_roundtrip(data: bytes) -> None:
    encoded = cobs_encode(data)
    assert b"\x00" not in encoded, "COBS-encoded bytes must not contain 0x00"
    assert cobs_decode(encoded) == data
