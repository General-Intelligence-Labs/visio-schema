"""Reference implementation of the Visio Ingest API's cryptography.

docs/protocol/ingest-api.md is the spec. A customer's service signs and
verifies with these functions; every client port is pinned to
tests/golden/ingest_vectors.txt. Advanced/internal by the repo's import model:
the stable public API is the package root.
"""

from .errors import (
    BAD_RECEIPT,
    BAD_REPLY,
    NOT_INGEST_MODE,
    RECORDING_IN_PROGRESS,
)
from .seal import CredentialsTampered, open_credentials, seal_credentials
from .signing import (
    ManifestEntry,
    authorization_header,
    canonical_query,
    content_digest,
    decode_secret,
    encode_secret,
    initiate_reply_signature,
    legacy_session_key,
    manifest_lines,
    request_signature,
    session_receipt,
    signatures_equal,
)

__all__ = [
    "BAD_RECEIPT",
    "BAD_REPLY",
    "NOT_INGEST_MODE",
    "RECORDING_IN_PROGRESS",
    "CredentialsTampered",
    "ManifestEntry",
    "authorization_header",
    "canonical_query",
    "content_digest",
    "decode_secret",
    "encode_secret",
    "initiate_reply_signature",
    "legacy_session_key",
    "manifest_lines",
    "open_credentials",
    "request_signature",
    "seal_credentials",
    "session_receipt",
    "signatures_equal",
]
