#!/usr/bin/env python3
"""Generate tests/golden/ingest_vectors.txt: the Ingest API's crypto vectors.

Every client port of python/visio_schema/ingest/ is pinned to these bytes. A
drift between ports does not fail loudly: the customer's service just answers
`bad_signature`, or the client refuses every receipt and never frees its card.
Committed vectors turn that into a build failure.

Format: the `key=lowercase_hexbytes` grammar every golden file uses. Text
values are their UTF-8 bytes in hex.

  secret                 the 32-byte test device secret, bytes 0x40..0x5f
  secret_b64url          its unpadded base64url wire form
  req.*                  one signed commit request: date, key_id, method,
                         path, query, body, body_sha256, string_to_sign,
                         signature, authorization
  query_req.*            the same fields for a request with a query string
  initiate.*             one initiate reply: device_id, request_date, body,
                         signature
  digest.parts           the manifest's `name:size:md5` lines, already in
                         digest order (by name's UTF-8 bytes)
  digest.value           its content_digest
  receipt.*              upload_id, session_key, value
  seal.*                 nonce (pinned; production is random), upload_id,
                         plaintext, key (the HKDF output), sealed (base64url)
  legacy.*               device_id, session_name, session_key

REGENERATION writes from the Python reference, so it proves cross-language
agreement, not correctness; python/tests/test_ingest.py checks the reference
against the spec's own construction with independent stdlib code.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "python"))

from visio_schema.ingest import seal, signing  # noqa: E402

OUT = _HERE.parent / "tests" / "golden" / "ingest_vectors.txt"

SECRET = bytes(range(0x40, 0x60))
DEVICE_ID = "e54a1de522f7e0f1"
KEY_ID = "rig-e54a1de5"
UPLOAD_ID = "up_01J9Z4QX"
SESSION_KEY = "8f1c2a0b9d3e4f5a6b7c8d9e0f1a2b3c"
SESSION_NAME = "session_00042-1787776719"
REQUEST_DATE = "20261009T031522Z"
SEAL_NONCE = bytes(range(0x60, 0x6C))

COMMIT_BODY = (
    b'{"device_id":"e54a1de522f7e0f1","sessions":[{"session_key":'
    b'"8f1c2a0b9d3e4f5a6b7c8d9e0f1a2b3c","session":"session_00042-1787776719",'
    b'"parts":[{"name":"ego_0000.mcap",'
    b'"bytes":268435456,"md5":"5f0c2b1e9d8c7b6a5f4e3d2c1b0a9f8e"}],'
    b'"metadata_file":{"name":"session.json","bytes":812,'
    b'"md5":"3c94e0a6b5c4d3e2f1a0b9c8d7e6f5a4"}}],'
    b'"remaining":{"sessions":0,"parts":0,"bytes":0}}'
)
INITIATE_REPLY = (
    b'{"protocol":"visio-ingest/1","upload_id":"up_01J9Z4QX",'
    b'"expires_at":"2026-10-16T03:15:22Z"}'
)
CREDENTIALS = b'{"secret_access_key":"s3cr3t","session_token":"tok"}'
# Out of order on purpose, and with one name a prefix of another: ordering by
# whole line would put `ego_0001.mcap.idx` first, ordering by name puts it last.
MANIFEST = (
    signing.ManifestEntry("ego_0001.mcap.idx", 4096, "0f1e2d3c4b5a69788796a5b4c3d2e1f0"),
    signing.ManifestEntry("ego_0001.mcap", 201326592, "a81d77c4f3e2d1c0b9a8978675645342"),
    signing.ManifestEntry("ego_0000.mcap", 268435456, "5f0c2b1e9d8c7b6a5f4e3d2c1b0a9f8e"),
)
QUERY = {"since": "2026", "limit": "10"}


def _request(path: str, query: str, body: bytes) -> list[tuple[str, bytes]]:
    req = dict(date=REQUEST_DATE, key_id=KEY_ID, method="POST", path=path,
               query=query)
    body_sha = signing.body_sha256_hex(body)
    sig = signing.request_signature(SECRET, body=body, **req)
    t = str.encode
    return [
        ("date", t(REQUEST_DATE)), ("key_id", t(KEY_ID)), ("method", b"POST"),
        ("path", t(path)), ("query", t(query)), ("body", body),
        ("body_sha256", t(body_sha)),
        ("string_to_sign", t(signing.request_string_to_sign(
            body_sha256=body_sha, **req))),
        ("signature", t(sig)),
        ("authorization", t(signing.authorization_header(KEY_ID, sig))),
    ]


def vectors() -> list[tuple[str, bytes]]:
    digest = signing.content_digest(MANIFEST)
    t = str.encode
    commit = _request(f"/visio/v1/uploads/{UPLOAD_ID}/commit", "", COMMIT_BODY)
    queried = _request("/visio/v1/uploads", signing.canonical_query(QUERY), b"")
    return [
        ("secret", SECRET),
        ("secret_b64url", t(signing.encode_secret(SECRET))),
        *((f"req.{k}", v) for k, v in commit),
        *((f"query_req.{k}", v) for k, v in queried),
        ("initiate.device_id", t(DEVICE_ID)),
        ("initiate.request_date", t(REQUEST_DATE)),
        ("initiate.body", INITIATE_REPLY),
        ("initiate.signature", t(signing.initiate_reply_signature(
            SECRET, device_id=DEVICE_ID, request_date=REQUEST_DATE,
            reply_body=INITIATE_REPLY))),
        ("digest.parts", t(signing.manifest_lines(MANIFEST))),
        ("digest.value", t(digest)),
        ("receipt.upload_id", t(UPLOAD_ID)),
        ("receipt.session_key", t(SESSION_KEY)),
        ("receipt.value", t(signing.session_receipt(
            SECRET, device_id=DEVICE_ID, upload_id=UPLOAD_ID,
            session_key=SESSION_KEY, content_digest=digest))),
        ("seal.nonce", SEAL_NONCE),
        ("seal.upload_id", t(UPLOAD_ID)),
        ("seal.plaintext", CREDENTIALS),
        ("seal.key", seal.credential_key(SECRET)),
        ("seal.sealed", t(seal._seal_with_nonce(
            SECRET, device_id=DEVICE_ID, upload_id=UPLOAD_ID,
            plaintext=CREDENTIALS, nonce=SEAL_NONCE))),
        ("legacy.device_id", t(DEVICE_ID)),
        ("legacy.session_name", t(SESSION_NAME)),
        ("legacy.session_key", t(signing.legacy_session_key(
            DEVICE_ID, SESSION_NAME))),
    ]


HEADER = """\
# Cross-language golden vectors for the Visio Ingest API's crypto
# (docs/protocol/ingest-api.md section 4.2). `key=hexbytes` per line; text
# values are their UTF-8 bytes. Generated by scripts/gen_ingest_vectors.py from
# python/visio_schema/ingest/ -- edit the generator, never this file. The
# secret is a TEST key (bytes 0x40..0x5f).
"""


def render() -> str:
    return HEADER + "".join(f"{k}={v.hex()}\n" for k, v in vectors())


if __name__ == "__main__":
    OUT.write_text(render())
    print(f"wrote {OUT}")
