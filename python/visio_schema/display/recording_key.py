"""Setting the recording key a rig encrypts its footage under.

visio-display is the ONLY place this key is ever handled. The client admin
mints it here on their own computer, sets it on a rig, and later opens that
rig's recordings with it. The capturer's phone never sees it: the app renders
one *Encrypted / Not encrypted* badge and offers no control, because the
operator holding the rig is the adversary the sealing exists to stop.

Nothing in this module returns key material to its caller. A change is
described going in, and what comes back is a sealed blob plus a fingerprint —
the same 16 hex the `VREC` header carries, so "what my device reports" and
"what opens this file" are one string rather than two encodings of it.

ORDERING IS THE SAFETY PROPERTY HERE. :func:`seal_key_change` remembers the new
key before it seals, so a key can never reach a device without first being on
the admin's keyring. The reverse order loses footage permanently the first time
a write fails.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from visio_schema.mcap.crypto import parse_key, remember_key
from visio_schema.settings_qr.sealed_body import (
    RECORDING_KEY_BYTES,
    RecordingKeyChangeRefused,
    SealedSecrets,
    recording_key_fingerprint,
    seal_secrets,
    validate_recording_key_change,
)
from visio_schema.v1.control import command_pb2

__all__ = [
    "CLEAR",
    "OPS",
    "ROTATE",
    "SET",
    "KeyChangeError",
    "SealedKeyChange",
    "command_for",
    "mint_key",
    "seal_key_change",
]

SET = "set"
ROTATE = "rotate"
CLEAR = "clear"
OPS = (SET, ROTATE, CLEAR)


class KeyChangeError(ValueError):
    """The requested change is malformed — refused before anything is sealed."""


@dataclass(frozen=True)
class SealedKeyChange:
    """A recording-key change, sealed and ready to send.

    `fingerprint` is what the device should report once it applies — empty for
    a clear. It is here so the caller can confirm the device landed on the key
    the admin intended, without ever holding the key to compare.
    """

    op: str
    blob: bytes
    fingerprint: str


def mint_key() -> bytes:
    """A fresh recording key from the OS CSPRNG."""
    return os.urandom(RECORDING_KEY_BYTES)


def _resolve_new_key(key: str | bytes | None) -> bytes:
    if key is None:
        return mint_key()
    if isinstance(key, bytes):
        if len(key) != RECORDING_KEY_BYTES:
            raise KeyChangeError(
                f"a recording key is {RECORDING_KEY_BYTES} bytes, "
                f"got {len(key)}")
        return key
    try:
        return parse_key(key, "the key given")
    except ValueError as exc:
        raise KeyChangeError(str(exc)) from exc


def _resolve_old_key(old_key: str | bytes | None) -> bytes | None:
    """The `rko` proof, parsed. Whether one is REQUIRED is not decided here.

    That rule lives in `validate_recording_key_change`, shared with the
    settings-QR generator: two implementations of "is this change well-formed"
    would let a QR do something this console refuses, and the difference would
    only show up on a rig.
    """
    if old_key is None:
        return None
    if isinstance(old_key, bytes):
        return old_key
    try:
        return parse_key(old_key, "the current key")
    except ValueError as exc:
        raise KeyChangeError(str(exc)) from exc


def seal_key_change(op: str,
                    *,
                    key: str | bytes | None = None,
                    old_key: str | bytes | None = None,
                    devices: Iterable[str] = ()) -> SealedKeyChange:
    """Remember the new key, then seal the change for one or more rigs.

    `devices` scopes the blob to those serials — the firmware refuses it on any
    other unit (`wrong_device`), so a blob captured off the wire cannot be
    replayed onto a rig it was not meant for. Pass the connected device's
    serial whenever it is known.

    Raises :class:`KeyChangeError` for a malformed request and
    :class:`~visio_schema.settings_qr.sealed_body.EnvelopeTooLarge` if the
    result would overrun the device's decode buffer.
    """
    if op not in OPS:
        raise KeyChangeError(f"unknown op {op!r} — one of {', '.join(OPS)}")

    devices = tuple(devices)
    if op == SET and old_key is not None:
        # A console affordance, not a device rule: the three buttons should
        # mean what they say. The device would accept this as a rotation.
        raise KeyChangeError(
            "set is for a rig that has no key yet — use rotate to replace one")
    old = _resolve_old_key(old_key)

    if op == CLEAR:
        # b"" is the wire's "clear", distinct from None ("leave alone").
        secrets = SealedSecrets(recording_key=b"", old_recording_key=old,
                                devices=devices)
        _check(secrets, first_provision=False)
        return SealedKeyChange(op, seal_secrets(secrets), "")

    new_key = _resolve_new_key(key)
    secrets = SealedSecrets(recording_key=new_key, old_recording_key=old,
                            devices=devices)
    # The SHARED rule, before anything is minted into the keyring or sealed.
    # `set` is this console's way of saying "these rigs have never been keyed".
    _check(secrets, first_provision=(op == SET))

    # BEFORE sealing, never after: a device must not be able to end up holding
    # a key the admin does not. A stray keyring entry from a send that then
    # failed is harmless; the opposite is unrecoverable.
    remember_key(new_key)

    return SealedKeyChange(op, seal_secrets(secrets),
                           recording_key_fingerprint(new_key))


def _check(secrets, *, first_provision: bool) -> None:
    """The shared refusal, in this console's vocabulary."""
    try:
        validate_recording_key_change(secrets, first_provision=first_provision)
    except RecordingKeyChangeRefused as exc:
        raise KeyChangeError(str(exc)) from exc


def command_for(change: SealedKeyChange) -> command_pb2.Command:
    """The bus command that applies `change`."""
    return command_pb2.Command(
        set_recording_key=command_pb2.SetRecordingKey(sealed=change.blob))
