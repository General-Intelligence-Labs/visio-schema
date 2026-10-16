"""Storage credentials sealed to one device (docs/protocol/ingest-api.md §4.2).

An initiate reply carries the secret half of the storage credentials as
`storage.sealed`: ChaCha20-Poly1305 under a key derived from the device's own
secret, with the device and upload ids as associated data. Only that device
can open it, and a sealed blob moved to another device or another upload fails
to open.

Unlike `visio_schema.crypto.envelope` (sealed to the fleet key, for QR codes),
this is symmetric: the customer's service holds the same device secret.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import b64url
from .signing import require_device_secret

__all__ = [
    "INFO",
    "NONCE_LEN",
    "TAG_LEN",
    "CredentialsTampered",
    "credential_key",
    "open_credentials",
    "seal_credentials",
]

INFO = b"visio-ingest upload v1"
NONCE_LEN = 12
TAG_LEN = 16    # Poly1305


class CredentialsTampered(Exception):
    """The blob is malformed, altered, or sealed to another device or upload."""


def credential_key(device_secret: bytes) -> bytes:
    """`HKDF-SHA256(ikm=device secret, salt="", info=INFO, L=32)`."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=INFO)
    return hkdf.derive(require_device_secret(device_secret))


def _aad(device_id: str, upload_id: str) -> bytes:
    return f"{device_id}\n{upload_id}".encode()


def _seal_with_nonce(
    device_secret: bytes, *, device_id: str, upload_id: str,
    plaintext: bytes, nonce: bytes,
) -> str:
    """The construction itself. Only the vector generator and the tests pin
    `nonce`; production always goes through `seal_credentials`, because a
    repeated nonce under ChaCha20-Poly1305 exposes both plaintexts."""
    if len(nonce) != NONCE_LEN:
        raise ValueError(f"nonce must be {NONCE_LEN} bytes")
    aead = ChaCha20Poly1305(credential_key(device_secret))
    return b64url.encode(
        nonce + aead.encrypt(nonce, plaintext, _aad(device_id, upload_id))
    )


def seal_credentials(
    device_secret: bytes, *, device_id: str, upload_id: str, plaintext: bytes
) -> str:
    """Unpadded base64url of `nonce || ciphertext || tag`, fresh nonce."""
    return _seal_with_nonce(
        device_secret, device_id=device_id, upload_id=upload_id,
        plaintext=plaintext, nonce=os.urandom(NONCE_LEN),
    )


def open_credentials(
    device_secret: bytes, *, device_id: str, upload_id: str, sealed: str
) -> bytes:
    """The sealed plaintext; CredentialsTampered if it will not open."""
    try:
        blob = b64url.decode(sealed)
    except ValueError as exc:
        raise CredentialsTampered("sealed credentials are not base64url") from exc
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise CredentialsTampered("sealed credentials are truncated")
    aead = ChaCha20Poly1305(credential_key(device_secret))
    try:
        return aead.decrypt(
            blob[:NONCE_LEN], blob[NONCE_LEN:], _aad(device_id, upload_id)
        )
    except InvalidTag as exc:
        raise CredentialsTampered("sealed credentials failed to open") from exc
