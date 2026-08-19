"""The secrets half of a v2 settings QR: what goes inside the envelope.

The sealed body is compact JSON, every field optional:

    { "sk":  "<storage secret, utf-8>",
      "rk":  "<base64 32 B - new recording key; \\"\\" clears>",
      "rko": "<base64 32 B - previous recording key, to rotate or clear>",
      "dev": ["GILABS-1a2b3c4d"],   # serials allowed to apply this QR
      "exp": 1786000000 }           # unix seconds, checked against device clock

`rk` has three states and they are not interchangeable:

    absent  -> leave the device's recording key alone (the common
               "just repoint my bucket" QR carries no key material at all)
    ""      -> clear the key; needs `rko` if one is already set
    32 B    -> set or rotate; rotating needs `rko`

`dev` and `exp` are the cheap answer to QR replay — a printed code is a
bearer token, and ~20 bytes each buys a customer who cares the ability to
mint one QR per rig, or one that stops working after a shift. Neither is on
by default.

**Parsing here is strict on purpose.** This module is the reference the
device-side C++ port is written against, so whatever it tolerates becomes
the spec. A body is attacker-supplied in the sense that matters: anyone can
seal one to the published fleet public key. Every field is type-checked, and
a malformed one raises rather than degrading — the failure that matters is
`rk` quietly becoming "" (stop encrypting), which is the destructive
direction.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field

from ..crypto import seal, unseal
from ..crypto.envelope import blob_key_id
from .payload import (
    PAYLOAD_TYPE,
    SEAL_DEVICES,
    SEAL_EXPIRES,
    SEAL_OLD_RECORDING_KEY,
    SEAL_RECORDING_KEY,
    SEAL_STORAGE_SECRET,
    SEALED_MAX_BYTES,
    SEALED_VERSION,
)

__all__ = [
    "RECORDING_KEY_BYTES",
    "EnvelopeTooLarge",
    "SealedSecrets",
    "open_sealed",
    "recording_key_fingerprint",
    "seal_into",
]

RECORDING_KEY_BYTES = 32


class EnvelopeTooLarge(ValueError):
    """The sealed blob exceeds the device's static decode buffer.

    Emitting it anyway would print a QR that scans perfectly and that every
    device silently discards: nanopb does not truncate an oversized field,
    it fails `pb_decode`, so the whole Command is dropped and nothing is
    answered.
    """


def _b64(raw: bytes) -> str:
    """base64url, no padding — the QR pays for every character."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str, where: str) -> bytes:
    """Strict base64url. `validate=False` (the default) DISCARDS characters
    outside the alphabet instead of raising, which would turn a corrupted
    field into silently different bytes rather than an error."""
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{where}: not valid base64 ({exc})") from exc


def _checked_b64_key(value: object, where: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{where}: must be a base64 string, got "
                         f"{type(value).__name__}")
    if not base64.urlsafe_b64encode(
            _unb64(value, where)).decode().rstrip("=") == value:
        raise ValueError(f"{where}: not canonical base64url")
    return _unb64(value, where)


def recording_key_fingerprint(key: bytes) -> str:
    """The 8-byte key fingerprint a device reports, as 16 lowercase hex.

    Deliberately the SAME bytes the `VREC` container writes into its
    `key_fp` header field, so an admin comparing "what my device says" to
    "what opens this file" is comparing one string to itself.
    """
    return hashlib.sha256(key).digest()[:8].hex()


@dataclass(frozen=True)
class SealedSecrets:
    """Everything a v2 QR hides from the operator who carries it."""

    storage_secret: str | None = None
    recording_key: bytes | None = None
    old_recording_key: bytes | None = None
    devices: tuple[str, ...] = field(default_factory=tuple)
    expires_at: int | None = None

    def __post_init__(self) -> None:
        if self.storage_secret is not None and not isinstance(
                self.storage_secret, str):
            raise ValueError("storage_secret must be a string")
        if self.recording_key is not None and self.recording_key != b"":
            self._require_key_length("recording_key", self.recording_key)
        if self.old_recording_key is not None:
            self._require_key_length("old_recording_key",
                                     self.old_recording_key)
        if not all(isinstance(d, str) for d in self.devices):
            raise ValueError("devices must be a sequence of serial strings")
        if self.expires_at is not None and (
                isinstance(self.expires_at, bool)
                or not isinstance(self.expires_at, int)):
            raise ValueError("expires_at must be unix seconds as an integer")

    @staticmethod
    def _require_key_length(name: str, key: object) -> None:
        if not isinstance(key, bytes) or len(key) != RECORDING_KEY_BYTES:
            raise ValueError(
                f"{name} must be exactly {RECORDING_KEY_BYTES} bytes, got "
                f"{len(key) if isinstance(key, bytes) else type(key).__name__}"
            )

    @property
    def sets_recording_key(self) -> bool:
        return bool(self.recording_key)

    @property
    def clears_recording_key(self) -> bool:
        return self.recording_key == b""

    @property
    def touches_recording_key(self) -> bool:
        return self.recording_key is not None

    @property
    def has(self) -> list[str]:
        """The cleartext routing hint: which apply-steps the app must run.

        Unauthenticated by construction — the app cannot read the envelope,
        so it needs *some* way to know whether to run the recording-key
        step. The device trusts nothing but the envelope's contents.
        """
        out = []
        if self.storage_secret is not None:
            out.append(SEAL_STORAGE_SECRET)
        if self.touches_recording_key:
            out.append(SEAL_RECORDING_KEY)
        return out

    def to_body(self) -> dict:
        body: dict = {}
        if self.storage_secret is not None:
            body[SEAL_STORAGE_SECRET] = self.storage_secret
        if self.touches_recording_key:
            body[SEAL_RECORDING_KEY] = (
                _b64(self.recording_key) if self.recording_key else "")
        if self.old_recording_key is not None:
            body[SEAL_OLD_RECORDING_KEY] = _b64(self.old_recording_key)
        if self.devices:
            body[SEAL_DEVICES] = list(self.devices)
        if self.expires_at is not None:
            body[SEAL_EXPIRES] = self.expires_at
        return body

    @property
    def is_empty(self) -> bool:
        """Nothing to seal — not even a replay guard.

        Deliberately NOT `not self.has`: `dev` and `exp` set no settings but
        are the entire point of a restricted QR, and dropping the envelope
        because "it sets nothing" would hand the operator an unrestricted,
        never-expiring code while they believed it was pinned to one rig.
        """
        return not self.to_body()

    @classmethod
    def from_body(cls, body: dict) -> SealedSecrets:
        if not isinstance(body, dict):
            raise ValueError("sealed body must be a JSON object")
        unknown = set(body) - {SEAL_STORAGE_SECRET, SEAL_RECORDING_KEY,
                               SEAL_OLD_RECORDING_KEY, SEAL_DEVICES,
                               SEAL_EXPIRES}
        if unknown:
            raise ValueError(f"sealed body has unknown fields: "
                             f"{sorted(unknown)}")

        rk = body.get(SEAL_RECORDING_KEY)
        if rk is None:
            recording_key = None
        elif rk == "":
            # Exact comparison, never truthiness: `0`/`false`/`[]` are all
            # falsy, and treating them as "" would silently turn a malformed
            # field into "stop encrypting".
            recording_key = b""
        else:
            recording_key = _checked_b64_key(rk, SEAL_RECORDING_KEY)

        rko = body.get(SEAL_OLD_RECORDING_KEY)
        devices = body.get(SEAL_DEVICES, ())
        if isinstance(devices, str):
            # A bare string is iterable, so tuple() would silently produce a
            # per-character device list that can never match a serial.
            raise ValueError(f"{SEAL_DEVICES}: must be a list of serials")
        return cls(
            storage_secret=body.get(SEAL_STORAGE_SECRET),
            recording_key=recording_key,
            old_recording_key=(None if rko is None
                               else _checked_b64_key(rko,
                                                     SEAL_OLD_RECORDING_KEY)),
            devices=tuple(devices),
            expires_at=body.get(SEAL_EXPIRES),
        )


def seal_into(cfg: dict, secrets: SealedSecrets, *,
              pubkey: bytes | None = None) -> dict:
    """A v1-shaped config + its secrets -> the v2 payload to print.

    Moves `storage.secret_access_key` out of the clear and into the
    envelope, so the caller cannot accidentally emit both. The returned dict
    is a copy; `cfg` is untouched.
    """
    if "sealed" in cfg:
        # Re-running the generator over its own output. A v2 payload has no
        # plaintext secret left to collect, so this would otherwise strip
        # the existing envelope and print a code that sets nothing.
        raise ValueError(
            "config already carries a sealed envelope — reprint from the "
            "original config, not from a generated payload"
        )

    # Discriminator first, so a v2 payload reads (and diffs, and pins as a
    # fixture) the same way every time regardless of config key order.
    out: dict = {"t": PAYLOAD_TYPE, "v": SEALED_VERSION}
    out.update({k: (dict(v) if isinstance(v, dict) else v)
                for k, v in cfg.items() if k not in ("t", "v")})

    storage = out.get("storage")
    if isinstance(storage, dict):
        storage.pop("secret_access_key", None)

    if secrets.is_empty:
        return out

    blob = seal(json.dumps(secrets.to_body(),
                           separators=(",", ":")).encode("utf-8"), pubkey)
    if len(blob) > SEALED_MAX_BYTES:
        raise EnvelopeTooLarge(
            f"sealed envelope is {len(blob)} B, over the device's "
            f"{SEALED_MAX_BYTES} B decode buffer — trim --device entries or "
            f"shorten the storage secret"
        )
    out["sealed"] = {
        "kid": blob_key_id(blob),
        "has": secrets.has,
        "b": _b64(blob),
    }
    return out


def open_sealed(payload: dict, privkey: bytes) -> SealedSecrets:
    """Open a v2 payload's envelope. The reference for the device-side C++.

    Only the fleet private key opens this, so it runs on a device or in a
    test — never in the companion app, which relays the blob verbatim.
    """
    sealed = payload.get("sealed")
    if not isinstance(sealed, dict):
        raise ValueError("payload has no sealed section")
    raw = unseal(_unb64(sealed["b"], "sealed.b"), privkey)
    return SealedSecrets.from_body(json.loads(raw.decode("utf-8")))
