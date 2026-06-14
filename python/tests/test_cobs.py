"""COBS encode/decode round-trips per visio-schema/docs/protocol/framing.md §3.2."""
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


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", b"\x01"),
        # 254 non-zero bytes end exactly on a 0xFF block: 0xFF code + 254 data,
        # and NO trailing block. A non-canonical encoder appends a phantom 0x01
        # here; this vector pins the canonical form.
        (b"\xff" * 254, b"\xff" * 255),
        # One past the boundary: full 0xFF block, then a 2-byte (0x02, 0xFF) block.
        (b"\xff" * 255, b"\xff" * 255 + b"\x02\xff"),
        # Two full 0xFF blocks back to back, again no trailing phantom block.
        (b"\xff" * 508, b"\xff" * 510),
    ],
)
def test_encode_golden_vectors(data: bytes, expected: bytes) -> None:
    """Exact-byte encodings, kept byte-identical with cpp/tests/test_cobs.cc."""
    assert cobs_encode(data) == expected
    assert cobs_decode(expected) == data
