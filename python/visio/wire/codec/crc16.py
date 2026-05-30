"""CRC-16/CCITT-FALSE per visio-schema/docs/framing.md §4.

Polynomial 0x1021, initial value 0xFFFF, no reflection, no XOR-out.
Check value: crc16(b"123456789") == 0x29B1.

The `crc` PyPI package's `Crc16.IBM_3740` is the same algorithm as
CCITT-FALSE (also known as CRC-16/AUTOSAR). Verified at module import.
"""
from __future__ import annotations

import threading

from crc import Calculator, Crc16

# `crc.Calculator.checksum` keeps a mutable register inside the instance,
# so a module-level singleton races when producer and consumer threads
# both call crc16() concurrently — observed as sporadic CRC mismatches in
# the pty high-volume test. Use a thread-local Calculator to keep the cost
# of construction low while ensuring per-thread state isolation.
_TLS = threading.local()


def _calc() -> Calculator:
    c = getattr(_TLS, "c", None)
    if c is None:
        c = Calculator(Crc16.IBM_3740)
        _TLS.c = c
    return c


def crc16(data: bytes) -> int:
    """Return CRC-16/CCITT-FALSE of `data` as a 16-bit unsigned int."""
    return _calc().checksum(data)


# Conformance gate per framing.md §4 — fail loudly at import if the
# underlying library is ever swapped for something with the wrong
# parameters.
assert crc16(b"123456789") == 0x29B1, "CRC-16/CCITT-FALSE check value mismatch"
