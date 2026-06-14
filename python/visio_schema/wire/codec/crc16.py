"""CRC-16/CCITT-FALSE per visio-schema/docs/protocol/framing.md §4.

Polynomial 0x1021, initial value 0xFFFF, no reflection, no XOR-out.
Check value: crc16(b"123456789") == 0x29B1.

`binascii.crc_hqx(data, 0xFFFF)` is the CRC-CCITT (poly 0x1021) seeded with
0xFFFF — exactly CCITT-FALSE, and the same value the C++ `Crc16`
(cpp/src/codec/crc16.cc) computes. It is the stdlib C path, so it stays off
the wire-close send/receive hot path (the old `crc` PyPI package was a
pure-Python bit-by-bit loop ~120x slower).
"""
from __future__ import annotations

from binascii import crc_hqx


def crc16(data: bytes) -> int:
    """Return CRC-16/CCITT-FALSE of `data` as a 16-bit unsigned int."""
    return crc_hqx(data, 0xFFFF)


# Conformance gate per framing.md §4 — fail loudly at import if the seed/poly
# ever drift away from CCITT-FALSE (wire-compat-critical with the C++ Crc16).
assert crc16(b"123456789") == 0x29B1, "CRC-16/CCITT-FALSE check value mismatch"
