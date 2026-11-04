"""Settings-QR sections for an Ingest API destination (docs/protocol/ingest-api.md).

A `storage` section whose endpoint is `visio+https://…` names a customer's
Ingest API instead of a bucket. The scheme is the whole selector, as the host
is for a cloud: there is no provider field. Such a section carries the
endpoint, the device key's id and an optional routing hint; the device key
itself is sealed (`sk`) in v2, exactly like a bucket secret.

The section's keys appear only when used, so a bucket-mode QR stays
byte-identical to what older apps parse.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from ..ingest.signing import decode_secret
from .fields import _err, _string

__all__ = [
    "BUCKET_ONLY_FIELDS",
    "INGEST_SCHEME",
    "INGEST_STORAGE_FIELDS",
    "is_ingest_endpoint",
    "validate_ingest_storage",
]

INGEST_SCHEME = "visio+https://"
INGEST_STORAGE_FIELDS = ("endpoint_url", "access_key_id", "destination_hint")
# Bucket-mode fields an Ingest API destination has no use for: the service
# chooses the bucket and folder, and status reports are off in this mode.
BUCKET_ONLY_FIELDS = ("region", "bucket", "prefix", "status_prefix")
_SECRET_WIRE_CHARS = 43   # 32 bytes as unpadded base64url


def is_ingest_endpoint(endpoint_url: object) -> bool:
    return isinstance(endpoint_url, str) and endpoint_url.startswith(INGEST_SCHEME)


def _device_secret(errs: list, where: str, value, *, required: bool) -> None:
    secret = _string(errs, where, value, required=required,
                     max_chars=_SECRET_WIRE_CHARS)
    if not secret:
        return
    try:
        decode_secret(secret)
    except ValueError:
        _err(errs, where, "deviceSecretFormat")


def validate_ingest_storage(errs: list, storage: dict, version: int, *,
                            plaintext: bool,
                            max_bytes: Mapping[str, int]) -> None:
    """A `storage` section whose endpoint is `visio+https://`.

    `plaintext` is true for a v1 payload, the only one that may carry the
    device key in clear. `max_bytes` is payload.STORAGE_MAX_BYTES.
    """
    allowed = INGEST_STORAGE_FIELDS
    if plaintext:
        allowed = (*INGEST_STORAGE_FIELDS, "secret_access_key")
    for f in storage:
        if f in allowed:
            continue
        if f == "secret_access_key":
            _err(errs, "storage.secret_access_key", "secretNotAllowedSealed",
                 version=version)
        elif f in BUCKET_ONLY_FIELDS:
            _err(errs, f"storage.{f}", "notForIngest")
        else:
            _err(errs, f"storage.{f}", "unknownField")
    endpoint = _string(errs, "storage.endpoint_url", storage.get("endpoint_url"),
                       required=True, max_bytes=max_bytes["endpoint_url"])
    try:
        host = urlsplit(endpoint).hostname
    except ValueError:
        host = None
    if not host:
        _err(errs, "storage.endpoint_url", "ingestNeedsHost")
    _string(errs, "storage.access_key_id", storage.get("access_key_id"),
            required=True, max_bytes=max_bytes["access_key_id"])
    _string(errs, "storage.destination_hint", storage.get("destination_hint"),
            max_bytes=max_bytes["destination_hint"])
    if plaintext:
        _device_secret(errs, "storage.secret_access_key",
                       storage.get("secret_access_key"), required=False)
