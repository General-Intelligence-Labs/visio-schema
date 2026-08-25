"""`visio-seal-v1` — the sealed-envelope format for settings QR codes.

A settings QR is a printed artifact handled by field operators, and the
operator is the adversary: they hold the rig, the SD card and the printout.
Anything readable on the code is readable by them. So the secrets inside a
QR (the storage secret key, the recording key) travel sealed to a published
fleet PUBLIC key, and only a device — which carries the private half baked
into its firmware image — can open them.

This is an ECIES / DHKEM-shaped construction:

    seal(pt, dev_pk):
      eph_sk, eph_pk = X25519_keygen()
      ss    = X25519(eph_sk, dev_pk)
      okm   = HKDF-SHA256(ikm=ss, salt="visio-seal-v1",
                          info=eph_pk || dev_pk, L=44)
      key   = okm[0:32];  nonce = okm[32:44]
      ct    = ChaCha20-Poly1305-Seal(key, nonce,
                                     aad="visio-seal-v1" || kid, pt)
      blob  = "VSL1"(4) || kid(4) || eph_pk(32) || ct(len(pt)+16)

Three properties worth stating, because each was a decision:

* **The nonce is derived, never random.** It is a function of a fresh
  ephemeral key, so it is unique per envelope and the *decrypt* side needs no
  RNG at all — which matters on a board whose entropy at early boot is not
  something to lean on.
* **`kid` is inside the AAD**, so an envelope cannot be relabelled for
  another key generation without failing the tag.
* **This is confidentiality, not authenticity.** The fleet public key is
  published to every customer, so anyone can mint a valid envelope. The tag
  makes a QR tamper-*evident*, not trustworthy. What stops an operator
  re-keying a rig is the device-side rule that rotating an existing
  recording key requires proving knowledge of the previous one — not
  anything in this file.

Not `libsodium crypto_box_seal`: its nonce is `blake2b-24(eph_pk||recip_pk)`,
and the OpenSSL 1.1.1h on the RV1106 exposes BLAKE2b only at 512-bit output
through EVP. Not RFC 9180 HPKE: `OSSL_HPKE_*` landed in OpenSSL 3.2. Not RSA:
a 2048-bit OAEP block is 256 B against a ~1800 B QR budget, where this
construction costs 48 B. The device-side C++ port speaks this same wire
format against the vectors in `tests/test_seal.py`.
"""

from __future__ import annotations

import functools
import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "MAGIC",
    "EnvelopeTampered",
    "MalformedEnvelope",
    "SealError",
    "WrongFleetKey",
    "blob_key_id",
    "fleet_public_key",
    "generate_keypair",
    "key_id",
    "load_public_key_pem",
    "public_key_pem",
    "seal",
    "unseal",
]

MAGIC = b"VSL1"
LABEL = b"visio-seal-v1"

_KID_LEN = 4
_KEY_LEN = 32       # X25519 raw scalar / point, and the ChaCha20 key
_NONCE_LEN = 12
_TAG_LEN = 16       # Poly1305
_HEADER_LEN = len(MAGIC) + _KID_LEN + _KEY_LEN

# The public key shipped in the wheel, so `pip install visio-schema` is all a
# fleet owner needs to seal a QR. Read lazily and cached: a consumer that only
# reads the wire contract never touches the filesystem for it.
_FLEET_KEY_PEM = Path(__file__).with_name("fleet_key.pem")


class SealError(Exception):
    """Base for every failure to open a sealed envelope."""


class MalformedEnvelope(SealError):
    """Not a `visio-seal-v1` blob at all — bad magic, or truncated."""


class WrongFleetKey(SealError):
    """Sealed to a different fleet key generation than the one supplied.

    Detected from the `kid` header BEFORE the AEAD runs, so this is
    distinguishable from tampering. That distinction is the whole point:
    it lets tooling say "no device of this fleet can open this code"
    instead of surfacing an indistinguishable MAC failure.
    """

    def __init__(self, want: str, have: str) -> None:
        super().__init__(
            f"envelope is sealed to fleet key {want}, but the key "
            f"supplied is {have}"
        )
        self.want = want
        self.have = have


class EnvelopeTampered(SealError):
    """Right key generation, but the ciphertext or its header was altered."""


def _raw_public(pubkey: bytes | X25519PublicKey) -> bytes:
    if isinstance(pubkey, X25519PublicKey):
        return pubkey.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    if len(pubkey) != _KEY_LEN:
        raise ValueError(f"X25519 public key must be {_KEY_LEN} bytes")
    return bytes(pubkey)


def key_id(pubkey: bytes | X25519PublicKey) -> str:
    """The 4-byte key generation id, lowercase hex.

    `SHA-256(raw_public_key)[:4]`. A device names its baked private key by
    this (`/etc/visio_seal/<kid>.key`), so an image can carry a retired
    generation alongside the current one and old QRs keep opening.
    """
    return hashlib.sha256(_raw_public(pubkey)).digest()[:_KID_LEN].hex()


def generate_keypair() -> tuple[bytes, bytes]:
    """A fresh X25519 `(private_raw, public_raw)` pair, 32 bytes each."""
    sk = X25519PrivateKey.generate()
    return (
        sk.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        ),
        _raw_public(sk.public_key()),
    )


def load_public_key_pem(data: bytes, source: str = "<pem>") -> bytes:
    """A PEM-encoded X25519 public key -> its 32 raw bytes.

    One owner for "parse a fleet public key", so the shipped key and a
    customer's `--pubkey` go through identical validation.
    """
    loaded = serialization.load_pem_public_key(data)
    if not isinstance(loaded, X25519PublicKey):
        raise ValueError(f"{source} is not an X25519 public key")
    return _raw_public(loaded)


def public_key_pem(pubkey: bytes | X25519PublicKey) -> bytes:
    """The PEM (SubjectPublicKeyInfo) encoding of an X25519 public key."""
    return X25519PublicKey.from_public_bytes(
        _raw_public(pubkey)
    ).public_bytes(serialization.Encoding.PEM,
                   serialization.PublicFormat.SubjectPublicKeyInfo)


@functools.lru_cache(maxsize=1)
def fleet_public_key() -> bytes:
    """The published fleet public key shipped in this wheel, 32 raw bytes."""
    return load_public_key_pem(_FLEET_KEY_PEM.read_bytes(), str(_FLEET_KEY_PEM))


def blob_key_id(blob: bytes) -> str:
    """The fleet key generation a sealed blob names, without opening it.

    Lets callers route or diagnose an envelope they cannot decrypt, without
    re-deriving the header layout from the format spec.
    """
    if len(blob) < len(MAGIC) + _KID_LEN or blob[: len(MAGIC)] != MAGIC:
        raise MalformedEnvelope("not a visio-seal-v1 envelope")
    return blob[len(MAGIC) : len(MAGIC) + _KID_LEN].hex()


def _derive(shared: bytes, eph_pub: bytes, peer_pub: bytes) -> tuple[bytes, bytes]:
    """`(key, nonce)` from the X25519 shared secret. Both sides run this."""
    okm = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN + _NONCE_LEN,
        salt=LABEL,
        info=eph_pub + peer_pub,
    ).derive(shared)
    return okm[:_KEY_LEN], okm[_KEY_LEN:]


def _seal_with_ephemeral(plaintext: bytes, peer_raw: bytes,
                         eph: X25519PrivateKey) -> bytes:
    """The envelope, with the ephemeral key supplied.

    Split out so the golden vectors in `tests/test_seal.py` can pin the SEAL
    direction against fixed bytes without putting that lever on the public
    API — reusing an ephemeral key across two envelopes reuses the derived
    nonce, which is a total loss of confidentiality for both.
    """
    eph_pub = _raw_public(eph.public_key())
    key, nonce = _derive(eph.exchange(X25519PublicKey.from_public_bytes(peer_raw)),
                         eph_pub, peer_raw)
    kid = bytes.fromhex(key_id(peer_raw))
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, LABEL + kid)
    return MAGIC + kid + eph_pub + ct


def seal(plaintext: bytes, pubkey: bytes | X25519PublicKey | None = None) -> bytes:
    """Seal `plaintext` to a fleet public key (default: the shipped one)."""
    peer_raw = fleet_public_key() if pubkey is None else _raw_public(pubkey)
    return _seal_with_ephemeral(plaintext, peer_raw, X25519PrivateKey.generate())


def unseal(blob: bytes, privkey: bytes | X25519PrivateKey) -> bytes:
    """Open a `visio-seal-v1` envelope with the fleet PRIVATE key.

    Raises `MalformedEnvelope`, `WrongFleetKey` or `EnvelopeTampered` — all
    subclasses of `SealError`, so a caller that does not care can catch the
    base. A bad *key argument* is a `ValueError` instead: that is a
    programming mistake, not a bad artifact, and must not be swallowed by
    the `except SealError` that reports an unreadable QR.
    """
    if not isinstance(privkey, X25519PrivateKey):
        if len(privkey) != _KEY_LEN:
            raise ValueError(f"X25519 private key must be {_KEY_LEN} bytes")
        privkey = X25519PrivateKey.from_private_bytes(bytes(privkey))

    # An empty plaintext still costs a tag, so this bound is exact, not a
    # guess: anything shorter cannot carry a complete envelope.
    if len(blob) < _HEADER_LEN + _TAG_LEN:
        raise MalformedEnvelope(
            f"envelope is {len(blob)} bytes, shorter than the minimum "
            f"{_HEADER_LEN + _TAG_LEN}"
        )
    if blob[: len(MAGIC)] != MAGIC:
        raise MalformedEnvelope(
            f"bad magic {blob[: len(MAGIC)]!r} (expected {MAGIC!r})"
        )

    kid = blob[len(MAGIC) : len(MAGIC) + _KID_LEN]
    eph_pub = blob[len(MAGIC) + _KID_LEN : _HEADER_LEN]
    ct = blob[_HEADER_LEN:]

    own_raw = _raw_public(privkey.public_key())
    own_kid = key_id(own_raw)
    if kid.hex() != own_kid:
        raise WrongFleetKey(want=kid.hex(), have=own_kid)

    try:
        shared = privkey.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    except ValueError as exc:
        # A low-order / all-zero ephemeral point makes the backend refuse to
        # compute a shared secret. That is a crafted envelope, not a caller
        # bug, so it belongs in the SealError taxonomy the docstring
        # promises — otherwise `except SealError` crashes on a QR anyone
        # can print against the published fleet key.
        raise MalformedEnvelope(
            f"envelope carries an unusable ephemeral key: {exc}"
        ) from exc

    key, nonce = _derive(shared, eph_pub, own_raw)
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ct, LABEL + kid)
    except InvalidTag as exc:
        raise EnvelopeTampered(
            "authentication tag failed — the envelope was altered"
        ) from exc
