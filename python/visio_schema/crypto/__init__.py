"""Sealed-envelope crypto for provisioning artifacts.

`visio-seal-v1` (see `envelope.py`) lets a fleet owner put secrets into a
printed settings QR that only a device can read. Advanced/internal by the
repo's import model — the stable public API is the package root; reach in
here deliberately.
"""

from .envelope import (
    MAGIC,
    EnvelopeTampered,
    MalformedEnvelope,
    SealError,
    WrongFleetKey,
    fleet_public_key,
    generate_keypair,
    key_id,
    seal,
    unseal,
)

__all__ = [
    "MAGIC",
    "EnvelopeTampered",
    "MalformedEnvelope",
    "SealError",
    "WrongFleetKey",
    "fleet_public_key",
    "generate_keypair",
    "key_id",
    "seal",
    "unseal",
]
