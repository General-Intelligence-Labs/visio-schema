"""Request and reply signatures of the Visio Ingest API.

The reference implementation of docs/protocol/ingest-api.md §4.2, and the
source of tests/golden/ingest_vectors.txt. Every client port is pinned to
those vectors, so a service written against this module agrees with every
client byte for byte.

Every signature is `hex(HMAC-SHA256(device_secret, string_to_sign))`, where
`device_secret` is the 32 bytes issued for one device, carried as unpadded
base64url. Inputs that a different port could encode differently (a lower-case
method, a query value holding `&`, a manifest name holding `:`) are refused
here rather than signed, so that ambiguity can never reach a signature.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..crypto import b64url

__all__ = [
    "ALGORITHM",
    "SECRET_LEN",
    "SESSION_KEY_HEX_LEN",
    "ManifestEntry",
    "authorization_header",
    "body_sha256_hex",
    "canonical_query",
    "content_digest",
    "decode_secret",
    "encode_secret",
    "initiate_reply_signature",
    "legacy_session_key",
    "manifest_lines",
    "request_signature",
    "request_string_to_sign",
    "require_device_secret",
    "session_receipt",
    "signatures_equal",
]

ALGORITHM = "VISIO-HMAC-SHA256"
SECRET_LEN = 32
SESSION_KEY_HEX_LEN = 32

_UPLOAD_LABEL = "VISIO-UPLOAD"
_COMMIT_LABEL = "VISIO-COMMIT"
_QUERY_RESERVED = ("&", "=")
_NAME_RESERVED = (":", "\n")


@dataclass(frozen=True)
class ManifestEntry:
    """One recording part as `content_digest` covers it."""

    name: str
    size: int
    md5: str


def require_device_secret(secret: bytes) -> bytes:
    if len(secret) != SECRET_LEN:
        raise ValueError(f"a device secret is {SECRET_LEN} bytes")
    return secret


def encode_secret(secret: bytes) -> str:
    """The wire form of a device secret: unpadded base64url (43 chars)."""
    return b64url.encode(require_device_secret(secret))


def decode_secret(text: str) -> bytes:
    """Parse a device secret; ValueError unless canonical and 32 bytes."""
    return require_device_secret(b64url.decode(text))


def body_sha256_hex(body: bytes) -> str:
    """`X-Visio-Content-SHA256`: hex SHA-256 of the exact request body."""
    return hashlib.sha256(body).hexdigest()


def canonical_query(params: Mapping[str, str]) -> str:
    """`k=v` pairs sorted by key and joined by `&`; empty when none.

    Keys and values are signed as sent, so neither may contain `&` or `=`:
    two different queries would otherwise sign the same string.
    """
    for k, v in params.items():
        if any(c in k or c in v for c in _QUERY_RESERVED):
            raise ValueError(f"query parameter {k!r} contains & or =")
    return "&".join(f"{k}={params[k]}" for k in sorted(params))


def request_string_to_sign(
    *,
    date: str,
    key_id: str,
    method: str,
    path: str,
    query: str,
    body_sha256: str,
) -> str:
    if method != method.upper():
        raise ValueError(f"method must be upper case, got {method!r}")
    return "\n".join((ALGORITHM, date, key_id, method, path, query, body_sha256))


def _hmac_hex(secret: bytes, text: str) -> str:
    return hmac.new(
        require_device_secret(secret), text.encode(), hashlib.sha256
    ).hexdigest()


def request_signature(
    secret: bytes,
    *,
    date: str,
    key_id: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
) -> str:
    """The `Signature=` value of a request's `Authorization` header."""
    return _hmac_hex(
        secret,
        request_string_to_sign(
            date=date,
            key_id=key_id,
            method=method,
            path=path,
            query=query,
            body_sha256=body_sha256_hex(body),
        ),
    )


def authorization_header(key_id: str, signature: str) -> str:
    return f"{ALGORITHM} Credential={key_id}, Signature={signature}"


def initiate_reply_signature(
    secret: bytes, *, device_id: str, request_date: str, reply_body: bytes
) -> str:
    """`X-Visio-Signature` on an initiate reply.

    Binds the reply to the request's own `X-Visio-Date`, so a recorded reply
    cannot be replayed to a later request.
    """
    text = "\n".join(
        (_UPLOAD_LABEL, device_id, request_date, body_sha256_hex(reply_body))
    )
    return _hmac_hex(secret, text)


def manifest_lines(parts: Sequence[ManifestEntry]) -> str:
    """The text `content_digest` hashes: one `name:size:md5\\n` line per
    part, ordered by the name's UTF-8 bytes (not by the whole line, which
    would put `a2` before `a`)."""
    for p in parts:
        if any(c in p.name for c in _NAME_RESERVED) or p.size < 0:
            raise ValueError(f"manifest entry {p.name!r} cannot be digested")
        if p.md5 and (len(p.md5) != 32 or any(c not in "0123456789abcdef" for c in p.md5)):
            raise ValueError(f"manifest entry {p.name!r} has an invalid MD5")
    ordered = sorted(parts, key=lambda p: p.name.encode())
    return "".join(f"{p.name}:{p.size}:{p.md5}\n" for p in ordered)


def content_digest(parts: Sequence[ManifestEntry]) -> str:
    """Hex SHA-256 of `manifest_lines(parts)`."""
    return hashlib.sha256(manifest_lines(parts).encode()).hexdigest()


def session_receipt(
    secret: bytes,
    *,
    device_id: str,
    upload_id: str,
    session_key: str,
    content_digest: str,
) -> str:
    """The `receipt` of a committed session: the client's licence to delete."""
    text = "\n".join(
        (_COMMIT_LABEL, device_id, upload_id, session_key, content_digest)
    )
    return _hmac_hex(secret, text)


def legacy_session_key(device_id: str, session_name: str) -> str:
    """The key of a session recorded before `session.json` carried one."""
    text = f"{device_id}\n{session_name}".encode()
    return hashlib.sha256(text).hexdigest()[:SESSION_KEY_HEX_LEN]


def signatures_equal(a: str, b: str) -> bool:
    """Constant-time comparison of two signatures taken off the wire."""
    return hmac.compare_digest(a.encode(), b.encode())
