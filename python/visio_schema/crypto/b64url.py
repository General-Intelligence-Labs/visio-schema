"""Unpadded base64url, decoded strictly.

Every secret, key and sealed blob this package puts on a wire or in a QR code
uses this one form. The decoder accepts exactly what the encoder produces: the
URL-safe alphabet, no padding, no whitespace, and canonical trailing bits.
`base64.urlsafe_b64decode` alone silently DISCARDS characters outside the
alphabet and ignores spare bits, so a corrupted value would become different
bytes instead of an error, and ports with strict decoders would disagree with
this reference about which inputs are valid.
"""

from __future__ import annotations

import base64
import binascii
import re

__all__ = ["decode", "encode"]

_ALPHABET = re.compile(r"[A-Za-z0-9_-]*")


def encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode(text: str) -> bytes:
    """The bytes `text` encodes; ValueError unless it is canonical."""
    if not _ALPHABET.fullmatch(text):
        raise ValueError("not unpadded base64url")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error as exc:
        raise ValueError("not unpadded base64url") from exc
    if encode(raw) != text:
        raise ValueError("not canonical base64url")
    return raw
