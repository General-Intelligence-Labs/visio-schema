"""What every settings-QR suite needs: the test fleet key and English notices.

Imported like `_golden` (a plain helper module, not conftest, whose name is
ambiguous across repos), so each suite states its fixtures instead of
inheriting them invisibly.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import _golden
import pytest

from visio_schema.settings_qr.i18n import set_language

_SEAL = _golden.load("seal_vectors.txt")
# The test fleet keypair the seal vectors pin. Never the shipped one.
TEST_PRIVATE = _SEAL["seal_private"]
TEST_PUBLIC = _SEAL["seal_public"]

_LOCALE_VARS = ("VISIO_LANG", "LC_ALL", "LC_MESSAGES", "LANG")


def pin_english(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """validate() and the CLI translate their notices, and the box running the
    suite may be a zh locale. Pin English so substring assertions read the
    text they expect regardless of $LANG."""
    for var in _LOCALE_VARS:
        monkeypatch.delenv(var, raising=False)
    set_language("en")
    yield
    set_language("en")


def write_test_pubkey_pem(directory: Path) -> str:
    """The TEST fleet public key as a PEM file, so a CLI run's output can be
    opened with TEST_PRIVATE instead of the shipped key."""
    from visio_schema.crypto.envelope import public_key_pem

    path = directory / "fleet.pem"
    path.write_bytes(public_key_pem(TEST_PUBLIC))
    return str(path)
