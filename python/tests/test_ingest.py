"""Ingest API crypto: the reference against the spec, and the golden vectors.

`gen_ingest_vectors.py` writes the vectors FROM the reference, so a vector
match alone proves nothing about correctness. These tests rebuild each
construction with independent stdlib code, straight from the spec text, and
pin the committed file to the generator so neither can drift alone.
"""

import base64
import hashlib
import hmac

import _golden
import pytest
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from visio_schema.ingest import (
    CredentialsTampered,
    ManifestEntry,
    authorization_header,
    canonical_query,
    content_digest,
    decode_secret,
    encode_secret,
    initiate_reply_signature,
    legacy_session_key,
    manifest_lines,
    open_credentials,
    request_signature,
    seal_credentials,
    session_receipt,
    signatures_equal,
)
from visio_schema.ingest.seal import _seal_with_nonce, credential_key

V = _golden.load("ingest_vectors.txt")
SECRET = V["secret"]
DEVICE_ID = V["initiate.device_id"].decode()
UPLOAD_ID = V["seal.upload_id"].decode()


def text(key: str) -> str:
    return V[key].decode()


def _hmac(text_to_sign: str) -> str:
    return hmac.new(SECRET, text_to_sign.encode(), hashlib.sha256).hexdigest()


def _hkdf_sha256(ikm: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 with an empty salt, from stdlib hmac only."""
    prk = hmac.new(b"", ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def test_committed_vectors_match_the_generator() -> None:
    gen = _golden.load_generator("gen_ingest_vectors")
    committed = (_golden.GOLDEN_DIR / "ingest_vectors.txt").read_text()
    assert committed == gen.render()


@pytest.mark.parametrize("prefix", ["req", "query_req"])
def test_request_signature_follows_the_spec(prefix: str) -> None:
    def field(name: str) -> str:
        return text(f"{prefix}.{name}")

    body = V[f"{prefix}.body"]
    sts = "\n".join([
        "VISIO-HMAC-SHA256", field("date"), field("key_id"), "POST",
        field("path"), field("query"), hashlib.sha256(body).hexdigest(),
    ])
    assert sts == field("string_to_sign")
    assert _hmac(sts) == field("signature")
    assert request_signature(
        SECRET, date=field("date"), key_id=field("key_id"), method="POST",
        path=field("path"), query=field("query"), body=body,
    ) == field("signature")
    assert field("authorization") == (
        f"VISIO-HMAC-SHA256 Credential={field('key_id')}, "
        f"Signature={field('signature')}")
    assert authorization_header(field("key_id"), field("signature")) == field(
        "authorization")


def test_canonical_query_sorts_by_key_and_joins() -> None:
    assert canonical_query({"b": "2", "a": "1"}) == "a=1&b=2"
    assert canonical_query({}) == ""
    assert text("query_req.query") == "limit=10&since=2026"


@pytest.mark.parametrize("params", [{"a": "1&b=2"}, {"a=b": "1"}])
def test_canonical_query_refuses_ambiguous_parameters(params) -> None:
    with pytest.raises(ValueError):
        canonical_query(params)


def test_a_lower_case_method_is_refused_not_normalised() -> None:
    with pytest.raises(ValueError):
        request_signature(SECRET, date="d", key_id="k", method="post",
                          path="/", query="", body=b"")


def test_initiate_reply_signature_follows_the_spec() -> None:
    body_sha = hashlib.sha256(V["initiate.body"]).hexdigest()
    expected = _hmac("\n".join([
        "VISIO-UPLOAD", DEVICE_ID, text("initiate.request_date"), body_sha]))
    assert expected == text("initiate.signature")
    later = initiate_reply_signature(
        SECRET, device_id=DEVICE_ID, request_date="20261009T031523Z",
        reply_body=V["initiate.body"])
    assert later != expected     # a recorded reply does not fit a new request


def test_receipt_and_digest_follow_the_spec() -> None:
    digest = hashlib.sha256(V["digest.parts"]).hexdigest()
    assert digest == text("digest.value")
    sts = "\n".join(["VISIO-COMMIT", DEVICE_ID, text("receipt.upload_id"),
                     text("receipt.session_key"), digest])
    assert text("receipt.value") == _hmac(sts)


def test_manifest_lines_order_by_name_not_by_line() -> None:
    gen = _golden.load_generator("gen_ingest_vectors")
    assert manifest_lines(gen.MANIFEST) == text("digest.parts")
    names = [line.split(":")[0] for line in text("digest.parts").splitlines()]
    assert names == ["ego_0000.mcap", "ego_0001.mcap", "ego_0001.mcap.idx"]
    assert content_digest(gen.MANIFEST) == text("digest.value")
    assert content_digest(list(reversed(gen.MANIFEST))) == text("digest.value")


@pytest.mark.parametrize("entry", [
    ManifestEntry("a:b.mcap", 1, "a" * 32),
    ManifestEntry("a\nb.mcap", 1, "a" * 32),
    ManifestEntry("a.mcap", -1, "a" * 32),
    ManifestEntry("a", 1, "a" * 32 + "\nb:2:" + "b" * 32),
    ManifestEntry("a.mcap", 1, "aa"),
    ManifestEntry("a.mcap", 1, "A" * 32),
])
def test_a_manifest_entry_that_could_collide_is_refused(entry) -> None:
    with pytest.raises(ValueError):
        content_digest([entry])


def test_legacy_manifest_may_omit_the_md5() -> None:
    assert manifest_lines([ManifestEntry("a.mcap", 1, "")]) == "a.mcap:1:\n"


def test_receipt_binds_every_field() -> None:
    base = dict(device_id="d", upload_id="u", session_key="k", content_digest="c")
    ref = session_receipt(SECRET, **base)
    for field in base:
        changed = {**base, field: base[field] + "x"}
        assert session_receipt(SECRET, **changed) != ref, field


def test_credential_key_is_rfc5869_hkdf() -> None:
    expected = _hkdf_sha256(SECRET, b"visio-ingest upload v1", 32)
    assert expected == V["seal.key"] == credential_key(SECRET)


def test_sealed_credentials_follow_the_spec() -> None:
    sealed = V["seal.sealed"]
    blob = base64.urlsafe_b64decode(sealed + b"=" * (-len(sealed) % 4))
    nonce, ct = blob[:12], blob[12:]
    assert nonce == V["seal.nonce"]
    aad = f"{DEVICE_ID}\n{UPLOAD_ID}".encode()
    key = _hkdf_sha256(SECRET, b"visio-ingest upload v1", 32)
    assert ChaCha20Poly1305(key).decrypt(nonce, ct, aad) == V["seal.plaintext"]
    assert open_credentials(SECRET, device_id=DEVICE_ID, upload_id=UPLOAD_ID,
                            sealed=text("seal.sealed")) == V["seal.plaintext"]


def _flip(sealed: str, index: int) -> str:
    blob = bytearray(base64.urlsafe_b64decode(sealed + "=" * (-len(sealed) % 4)))
    blob[index] ^= 1
    return base64.urlsafe_b64encode(bytes(blob)).rstrip(b"=").decode()


@pytest.mark.parametrize("secret, device_id, upload_id, sealed", [
    (SECRET, "other", UPLOAD_ID, text("seal.sealed")),
    (SECRET, DEVICE_ID, "up_other", text("seal.sealed")),
    (bytes(32), DEVICE_ID, UPLOAD_ID, text("seal.sealed")),
    (SECRET, DEVICE_ID, UPLOAD_ID, _flip(text("seal.sealed"), 20)),
    (SECRET, DEVICE_ID, UPLOAD_ID, _flip(text("seal.sealed"), -1)),
    (SECRET, DEVICE_ID, UPLOAD_ID, "AAAA"),
    (SECRET, DEVICE_ID, UPLOAD_ID, "A"),
    (SECRET, DEVICE_ID, UPLOAD_ID, text("seal.sealed") + "!"),
    (SECRET, DEVICE_ID, UPLOAD_ID, "封"),
])
def test_credentials_that_will_not_open_say_so(secret, device_id, upload_id,
                                               sealed) -> None:
    with pytest.raises(CredentialsTampered):
        open_credentials(secret, device_id=device_id, upload_id=upload_id,
                         sealed=sealed)


def test_seal_round_trips_with_a_fresh_nonce() -> None:
    a = seal_credentials(SECRET, device_id="d", upload_id="u", plaintext=b"{}")
    b = seal_credentials(SECRET, device_id="d", upload_id="u", plaintext=b"{}")
    assert a != b
    assert open_credentials(SECRET, device_id="d", upload_id="u", sealed=a) == b"{}"


@pytest.mark.parametrize("nonce", [bytes(11), bytes(13)])
def test_a_wrong_length_nonce_is_refused(nonce: bytes) -> None:
    with pytest.raises(ValueError):
        _seal_with_nonce(SECRET, device_id="d", upload_id="u", plaintext=b"",
                         nonce=nonce)


def test_secret_wire_form_round_trips() -> None:
    wire = encode_secret(SECRET)
    assert len(wire) == 43 and wire == text("secret_b64url")
    assert decode_secret(wire) == SECRET


_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _non_canonical_last_char(wire: str) -> str:
    # The 43rd character carries 2 spare bits; a neighbour with the same top
    # 4 bits decodes to the same bytes and must still be refused.
    return wire[:-1] + _ALPHABET[_ALPHABET.index(wire[-1]) ^ 1]


def _standard_alphabet(wire: str) -> str:
    return "+" + wire[1:]


@pytest.mark.parametrize("make", [
    lambda w: w + "=",
    lambda w: " " + w,
    lambda w: w + "\n",
    lambda w: w[:20] + "!!" + w[20:],
    _standard_alphabet,
    _non_canonical_last_char,
    lambda w: w[:-1],
    lambda w: "A",
    lambda w: "AAAA",
    lambda w: w[:-1] + "封",
])
def test_a_non_canonical_secret_is_refused(make) -> None:
    with pytest.raises(ValueError):
        decode_secret(make(text("secret_b64url")))


@pytest.mark.parametrize("length", [31, 33])
def test_a_wrong_length_secret_is_refused_everywhere(length: int) -> None:
    with pytest.raises(ValueError):
        encode_secret(bytes(length))
    with pytest.raises(ValueError):
        credential_key(bytes(length))
    with pytest.raises(ValueError):
        session_receipt(bytes(length), device_id="d", upload_id="u",
                        session_key="k", content_digest="c")


def test_legacy_session_key_is_32_hex_of_the_spec_hash() -> None:
    expected = hashlib.sha256(
        f"{text('legacy.device_id')}\n{text('legacy.session_name')}".encode()
    ).hexdigest()[:32]
    assert legacy_session_key(
        text("legacy.device_id"), text("legacy.session_name")
    ) == expected == text("legacy.session_key")


def test_signature_comparison_is_exact_and_never_raises() -> None:
    assert signatures_equal("ab", "ab")
    assert not signatures_equal("ab", "ac")
    assert not signatures_equal("ab", "abc")
    assert not signatures_equal("ab", "封")
