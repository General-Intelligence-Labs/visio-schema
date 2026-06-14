"""COBS framing for serial transport per visio-schema/docs/protocol/framing.md §3.2.

A COBS-encoded frame on the wire is `cobs.encode(payload) || 0x00`. The
trailing 0x00 is the frame delimiter; COBS encoding guarantees no other
0x00 bytes appear in the encoded run, so readers can scan for it.

This module is a thin wrapper around the `cobs` PyPI package. The
encoded-bytes-only API of that package is the right shape for our use
case — the delimiter is the transport layer's concern, not the codec's.
"""
from __future__ import annotations

from cobs import cobs as _cobs


def cobs_encode(data: bytes) -> bytes:
    """Return the COBS-encoded bytes for `data` (no trailing 0x00 delimiter)."""
    return _cobs.encode(data)


def cobs_decode(encoded: bytes) -> bytes:
    """COBS-decode `encoded` (must NOT include a trailing 0x00 delimiter)."""
    return _cobs.decode(encoded)
