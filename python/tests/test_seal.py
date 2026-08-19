"""`visio-seal-v1` envelope tests, including the cross-language golden vector.

The vectors in `tests/golden/seal_vectors.txt` are the single most important
fixture in the sealed-QR work.
The device opens these envelopes with a C++ port of `envelope.py` against
the vendor OpenSSL 1.1.1h, and a drift between the two does not
fail loudly in CI — it fails in a factory, where the symptom is "the QR
doesn't work" on a rig somebody already shipped. Pinning both directions
against fixed bytes is what makes that a build failure instead.

They live in the same committed-fixture form as `wire_vectors.txt`, so the
device-side port reads ONE file rather than hex transcribed by hand into
another repo.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from visio_schema.crypto import (
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
from visio_schema.crypto.envelope import (
    _seal_with_ephemeral,
    blob_key_id,
    load_public_key_pem,
    public_key_pem,
)

_GOLDEN = (Path(__file__).resolve().parents[1].parent
           / "tests" / "golden" / "seal_vectors.txt")


def _load() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for line in _GOLDEN.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, hexbytes = line.split("=", 1)
        out[key] = bytes.fromhex(hexbytes)
    return out


_V = _load()
TEST_PRIVATE = _V["seal_private"]
TEST_PUBLIC = _V["seal_public"]
TEST_EPHEMERAL = _V["seal_ephemeral"]
TEST_KID = _V["seal_kid"].hex()
GOLDEN_PLAINTEXT = _V["seal_plaintext"]
GOLDEN_BLOB = _V["seal_blob"]

# magic(4) + kid(4) + ephemeral pubkey(32) + Poly1305 tag(16).
ENVELOPE_OVERHEAD = 56


def test_golden_vector_seals_to_exactly_these_bytes() -> None:
    """The SEAL direction, pinned. A C++ port must reproduce this."""
    eph = X25519PrivateKey.from_private_bytes(TEST_EPHEMERAL)
    assert _seal_with_ephemeral(GOLDEN_PLAINTEXT, TEST_PUBLIC, eph) == GOLDEN_BLOB


def test_golden_vector_unseals_to_exactly_these_bytes() -> None:
    """The UNSEAL direction — what the device actually runs."""
    assert unseal(GOLDEN_BLOB, TEST_PRIVATE) == GOLDEN_PLAINTEXT


def test_golden_blob_layout() -> None:
    assert GOLDEN_BLOB[:4] == MAGIC
    assert GOLDEN_BLOB[4:8].hex() == TEST_KID
    assert len(GOLDEN_BLOB) == len(GOLDEN_PLAINTEXT) + ENVELOPE_OVERHEAD


def test_key_id_is_derived_from_the_public_key() -> None:
    assert key_id(TEST_PUBLIC) == TEST_KID


@pytest.mark.parametrize("size", [0, 1, 200, 1024])
def test_round_trip_at_several_sizes(size: int) -> None:
    priv, pub = generate_keypair()
    payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    assert unseal(seal(payload, pub), priv) == payload


def test_overhead_is_constant_regardless_of_payload() -> None:
    _, pub = generate_keypair()
    for size in (0, 17, 250):
        assert len(seal(b"x" * size, pub)) - size == ENVELOPE_OVERHEAD


def test_two_seals_of_the_same_plaintext_differ() -> None:
    """A fresh ephemeral key per envelope, so the derived nonce never repeats."""
    _, pub = generate_keypair()
    assert seal(b"same", pub) != seal(b"same", pub)


def test_wrong_fleet_key_is_reported_before_the_aead() -> None:
    """`bad_envelope` vs `bad_old_key` is a support-call-sized distinction.

    A tool must be able to say "no device of this fleet can open this code"
    rather than surface an indistinguishable MAC failure.
    """
    other_priv, _ = generate_keypair()
    with pytest.raises(WrongFleetKey) as exc:
        unseal(GOLDEN_BLOB, other_priv)
    assert exc.value.want == TEST_KID
    assert exc.value.have != TEST_KID


def test_relabelling_the_kid_is_rejected() -> None:
    """`kid` is inside the AAD, so an envelope cannot be moved between
    generations even by someone who also owns the other generation."""
    tampered = bytearray(GOLDEN_BLOB)
    tampered[4] ^= 0xFF
    with pytest.raises(WrongFleetKey):
        unseal(bytes(tampered), TEST_PRIVATE)


@pytest.mark.parametrize("index", [8, 40, 100, -1])
def test_any_flipped_bit_fails_the_tag(index: int) -> None:
    tampered = bytearray(GOLDEN_BLOB)
    tampered[index] ^= 0x01
    with pytest.raises(EnvelopeTampered):
        unseal(bytes(tampered), TEST_PRIVATE)


def test_bad_magic_is_not_confused_with_tampering() -> None:
    with pytest.raises(MalformedEnvelope):
        unseal(b"NOPE" + GOLDEN_BLOB[4:], TEST_PRIVATE)


@pytest.mark.parametrize("length", [0, 4, 55])
def test_truncated_envelope(length: int) -> None:
    with pytest.raises(MalformedEnvelope):
        unseal(GOLDEN_BLOB[:length], TEST_PRIVATE)


def test_every_failure_is_a_seal_error() -> None:
    """Callers that do not care about the distinction catch one base class."""
    for exc in (MalformedEnvelope, WrongFleetKey, EnvelopeTampered):
        assert issubclass(exc, SealError)


def test_wrong_key_length_is_a_value_error_not_a_seal_error() -> None:
    # A programming mistake, not a bad artifact — it should not be swallowed
    # by an `except SealError` that is there to report a bad QR.
    with pytest.raises(ValueError):
        unseal(GOLDEN_BLOB, b"too short")
    with pytest.raises(ValueError):
        seal(b"x", b"too short")


class TestShippedFleetKey:
    """The published key in the wheel — what `pip install visio-schema` gets."""

    def test_it_loads_and_is_32_raw_bytes(self) -> None:
        assert len(fleet_public_key()) == 32

    def test_it_is_cached_not_re_read(self) -> None:
        assert fleet_public_key() is fleet_public_key()

    def test_sealing_to_it_by_default_matches_naming_it(self) -> None:
        blob = seal(b"hello")
        assert blob[4:8].hex() == key_id(fleet_public_key())

    def test_the_private_half_is_not_in_this_repo(self) -> None:
        """The wheel ships the PUBLIC key only. If a private key ever lands
        here it would be published to PyPI on the next release."""
        from visio_schema.crypto.envelope import _FLEET_KEY_PEM

        text = _FLEET_KEY_PEM.read_text()
        assert "PRIVATE KEY" not in text
        assert "BEGIN PUBLIC KEY" in text


class TestCraftedEnvelopes:
    """An envelope is attacker-supplied: the fleet PUBLIC key is published,
    so anyone can mint one and print it on a sticker."""

    def test_a_low_order_ephemeral_key_stays_inside_the_taxonomy(self) -> None:
        """An all-zero ephemeral point makes the backend refuse to compute a
        shared secret. That must surface as a SealError, or a caller doing
        `except SealError` to report a bad QR crashes instead."""
        crafted = GOLDEN_BLOB[:8] + b"\x00" * 32 + GOLDEN_BLOB[40:]
        with pytest.raises(MalformedEnvelope):
            unseal(crafted, TEST_PRIVATE)

    def test_every_single_byte_corruption_is_a_seal_error(self) -> None:
        """Exhaustive, not sampled: no offset may escape the taxonomy."""
        for i in range(len(GOLDEN_BLOB)):
            crafted = bytearray(GOLDEN_BLOB)
            crafted[i] ^= 0xFF
            with pytest.raises(SealError):
                unseal(bytes(crafted), TEST_PRIVATE)


class TestBlobKeyId:
    def test_it_reads_the_generation_without_the_private_key(self) -> None:
        assert blob_key_id(GOLDEN_BLOB) == TEST_KID

    @pytest.mark.parametrize("bad", [b"", b"VSL1", b"NOPE" + b"\x00" * 8])
    def test_it_rejects_a_non_envelope(self, bad: bytes) -> None:
        with pytest.raises(MalformedEnvelope):
            blob_key_id(bad)


class TestPemHelpers:
    def test_public_key_pem_round_trips(self) -> None:
        assert load_public_key_pem(public_key_pem(TEST_PUBLIC)) == TEST_PUBLIC

    def test_a_non_x25519_pem_is_rejected_by_name(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        pem = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        with pytest.raises(ValueError, match="not an X25519 public key"):
            load_public_key_pem(pem, "ed25519.pem")
