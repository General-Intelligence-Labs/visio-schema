"""Reference rig client for the Ingest API: what the firmware does, in Python.

For server integration tests and staging, and as a readable statement of the
rig's side of the protocol
(``docs/protocol/ingest-api.md``): sign every call, correct the clock from ``Date``
on ``clock_skew``, verify the ``initiate`` signature before using the folder,
open the sealed credentials, PUT each file once while hashing it, then commit and
treat a session as done **only** on a receipt that verifies.

Standard library only (``urllib``), like the rest of the package's runtime. The
storage PUT covers the three credential types: ``session_token`` and ``static``
are S3-compatible SigV4 (AWS, R2, MinIO, OSS/COS in their S3 dialect), and
``azure_sas`` appends the SAS to the blob URL. Not a production uploader: no
pacing, no multi-run queue, no tombstones.

    client = IngestClient.from_endpoint(
        "visio+https://api.example.com/api/v1/device-upload",
        key_id="dk_...", secret=decode_secret("..."), device_id="e54a1de522f7e0f1")
    upload = client.initiate(device_kind="ego", sessions=1, parts=2, bytes=...)
    results = client.upload_session(upload, Path("session_00042-1786166461"))
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .seal import open_credentials
from .signing import (
    authorization_header,
    body_sha256_hex,
    initiate_reply_signature,
    request_signature,
    session_receipt,
    signatures_equal,
)

__all__ = [
    "CommitRejected",
    "IngestClient",
    "IngestError",
    "Response",
    "SessionFiles",
    "Upload",
    "describe",
    "new_session_key",
    "session_files",
]

METADATA_FILE = "session.json"
_CHUNK = 1024 * 1024

HEADER_DATE = "X-Visio-Date"
HEADER_BODY_SHA256 = "X-Visio-Content-SHA256"
DATE_FORMAT = "%Y%m%dT%H%M%SZ"


def sign_request(
    *, secret: bytes, key_id: str, method: str, path: str, body: bytes, date: datetime
) -> dict[str, str]:
    """The three auth headers a rig sends (ingest-api.md §4.2)."""
    stamp = date.astimezone(timezone.utc).strftime(DATE_FORMAT)
    signature = request_signature(
        secret, date=stamp, key_id=key_id, method=method, path=path, query="", body=body)
    return {
        HEADER_DATE: stamp,
        HEADER_BODY_SHA256: body_sha256_hex(body),
        "Authorization": authorization_header(key_id, signature),
    }


class IngestError(Exception):
    """A request-level error from the API: its status and protocol ``code``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class CommitRejected(Exception):
    """A reply the rig must not act on (bad signature or receipt)."""


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


# (method, url, headers, body) -> Response. Injectable so tests need no network.
Transport = Callable[[str, str, dict[str, str], bytes], Response]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes) -> Response:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            return Response(reply.status, {k.lower(): v for k, v in reply.headers.items()},
                            reply.read())
    except urllib.error.HTTPError as err:
        return Response(err.code, {k.lower(): v for k, v in err.headers.items()}, err.read())


def new_session_key() -> str:
    """A fresh random ``session_key``: 32 lowercase hex, never derived from content."""
    return secrets.token_hex(16)


@dataclass
class Upload:
    """A verified ``initiate`` reply, with its credentials opened."""

    upload_id: str
    expires_at: str
    storage: dict[str, Any]
    credentials: dict[str, str] = field(repr=False)

    @property
    def prefix(self) -> str:
        return self.storage.get("prefix", "")


@dataclass
class SessionFiles:
    """One session folder on disk: its MCAP parts, then its ``session.json``."""

    session: str
    parts: list[Path]
    metadata: Path


def session_files(folder: Path) -> SessionFiles:
    """The files a session folder uploads, parts in name order, metadata last."""
    metadata = folder / METADATA_FILE
    if not metadata.is_file():
        raise FileNotFoundError(f"{folder} has no {METADATA_FILE}")
    parts = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix == ".mcap")
    if not parts:
        raise FileNotFoundError(f"{folder} has no .mcap parts")
    return SessionFiles(folder.name, parts, metadata)


def _md5_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.md5()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# --- S3 SigV4 (path-style), just enough for one PUT -------------------------------


def _sigv4_put_headers(
    *,
    url: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    session_token: str | None,
    payload_sha256: str,
    content_md5_b64: str,
    when: datetime,
) -> dict[str, str]:
    parsed = urllib.parse.urlsplit(url)
    amz_date = when.strftime("%Y%m%dT%H%M%SZ")
    day = when.strftime("%Y%m%d")
    headers = {
        "host": parsed.netloc,
        "content-md5": content_md5_b64,
        "x-amz-content-sha256": payload_sha256,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed = sorted(headers)
    canonical = "\n".join([
        "PUT",
        urllib.parse.quote(parsed.path or "/", safe="/-_.~"),
        parsed.query,
        "".join(f"{name}:{headers[name].strip()}\n" for name in signed),
        ";".join(signed),
        payload_sha256,
    ])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = _h(_h(_h(_h(f"AWS4{secret_access_key}".encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={';'.join(signed)}, Signature={signature}"
    )
    return headers


class IngestClient:
    """One rig talking to one Ingest API."""

    def __init__(
        self,
        base_url: str,
        *,
        key_id: str,
        secret: bytes,
        device_id: str,
        transport: Transport = urllib_transport,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.secret = secret
        self.device_id = device_id
        self.transport = transport
        self._clock = clock
        # The rig has no trustworthy wall clock; it keeps an offset learned
        # from the server's Date header.
        self.clock_offset = timedelta(0)

    @classmethod
    def from_endpoint(cls, endpoint_url: str, **kwargs: Any) -> IngestClient:
        """From a ``visio+https://`` (or ``visio+http://``) endpoint, as provisioned."""
        if not endpoint_url.startswith(("visio+https://", "visio+http://")):
            raise ValueError("an Ingest API endpoint starts with visio+https://")
        return cls(endpoint_url[len("visio+"):], **kwargs)

    def now(self) -> datetime:
        return self._clock() + self.clock_offset

    # --- signed calls ---------------------------------------------------------

    def _call(self, path: str, body: dict[str, Any]) -> tuple[Response, str]:
        raw = json.dumps(body, separators=(",", ":")).encode()
        full_path = urllib.parse.urlsplit(self.base_url).path + path
        for attempt in (1, 2):
            headers = sign_request(
                secret=self.secret, key_id=self.key_id, method="POST", path=full_path,
                body=raw, date=self.now())
            headers["Content-Type"] = "application/json"
            reply = self.transport("POST", self.base_url + path, headers, raw)
            if reply.status == 401 and attempt == 1 and _code(reply) == "clock_skew":
                self._learn_clock(reply)
                continue
            if reply.status >= 400:
                error = (reply.json() or {}).get("error") or {}
                raise IngestError(reply.status, error.get("code", ""), error.get("message", ""))
            return reply, headers[HEADER_DATE]
        raise AssertionError("unreachable")

    def _learn_clock(self, reply: Response) -> None:
        date = reply.headers.get("date")
        if not date:
            return
        server = email.utils.parsedate_to_datetime(date)
        self.clock_offset += server - self.now()

    def initiate(self, **fields: Any) -> Upload:
        """``POST /v1/uploads/initiate``; raises ``CommitRejected`` on a bad signature."""
        body = {
            "device_id": self.device_id,
            "credential_types": ["static", "session_token", "azure_sas"],
            "previous_upload_id": None,
            "test": False,
            **fields,
        }
        reply, sent_date = self._call("/v1/uploads/initiate", body)
        expected = initiate_reply_signature(
            self.secret, device_id=self.device_id, request_date=sent_date, reply_body=reply.body)
        if not signatures_equal(reply.headers.get("x-visio-signature", ""), expected):
            raise CommitRejected("initiate reply signature does not verify")
        payload = reply.json()
        if payload.get("upload_id") is None:  # a test initiate
            return Upload("", "", payload.get("storage") or {}, {})
        storage = payload["storage"]
        opened = open_credentials(
            self.secret, device_id=self.device_id, upload_id=payload["upload_id"],
            sealed=storage["sealed"])
        return Upload(payload["upload_id"], payload["expires_at"], storage, json.loads(opened))

    def commit(
        self,
        upload: Upload,
        sessions: list[dict[str, Any]],
        remaining: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/uploads/{id}/commit``; every ``committed`` receipt is verified."""
        body = {
            "device_id": self.device_id,
            "sessions": sessions,
            "remaining": remaining or {"sessions": 0, "parts": 0, "bytes": 0},
        }
        reply, _ = self._call(f"/v1/uploads/{upload.upload_id}/commit", body)
        payload = reply.json()
        for entry in payload.get("sessions", []):
            if entry.get("state") != "committed":
                continue
            expected = session_receipt(
                self.secret, device_id=self.device_id, upload_id=upload.upload_id,
                session_key=entry["session_key"], content_digest=entry["content_digest"])
            if not signatures_equal(entry.get("receipt", ""), expected):
                raise CommitRejected(f"receipt for {entry['session_key']} does not verify")
        return payload

    # --- storage --------------------------------------------------------------

    def put(self, upload: Upload, key: str, path: Path) -> str:
        """PUT one file to the assigned folder; returns its MD5 (hex)."""
        data = path.read_bytes()
        md5 = hashlib.md5(data)
        storage, creds = upload.storage, upload.credentials
        object_key = f"{upload.prefix}{key}"
        if "sas" in creds:
            url = (f"{storage['endpoint_url'].rstrip('/')}/{storage['bucket']}/"
                   f"{urllib.parse.quote(object_key)}?{creds['sas']}")
            headers = {"x-ms-blob-type": "BlockBlob",
                       "Content-MD5": base64.b64encode(md5.digest()).decode()}
        else:
            url = (f"{storage['endpoint_url'].rstrip('/')}/{storage['bucket']}/"
                   f"{urllib.parse.quote(object_key)}")
            headers = _sigv4_put_headers(
                url=url, region=storage.get("region") or "auto",
                access_key_id=storage["access_key_id"],
                secret_access_key=creds["secret_access_key"],
                session_token=creds.get("session_token"),
                payload_sha256=hashlib.sha256(data).hexdigest(),
                content_md5_b64=base64.b64encode(md5.digest()).decode(),
                when=self.now().astimezone(timezone.utc),
            )
        # Explicit: urllib otherwise labels a PUT body as a form, which some
        # servers then parse and discard (moto stores 0 bytes).
        headers["content-type"] = "application/octet-stream"
        reply = self.transport("PUT", url, headers, data)
        if reply.status not in (200, 201):
            raise IngestError(reply.status, "storage", reply.body[:300].decode(errors="replace"))
        return md5.hexdigest()

    def upload_session(
        self, upload: Upload, folder: Path, *, session_key: str | None = None
    ) -> dict[str, Any]:
        """PUT a session's parts then its ``session.json``, and commit it alone.

        Returns the session's commit entry. The caller may delete the folder
        only when it says ``committed`` (the receipt was verified by ``commit``).
        """
        files = session_files(folder)
        key = session_key or new_session_key()
        parts = []
        for path in files.parts:
            md5 = self.put(upload, f"{key}/{path.name}", path)
            parts.append({"name": path.name, "bytes": path.stat().st_size, "md5": md5})
        meta_md5 = self.put(upload, f"{key}/{METADATA_FILE}", files.metadata)
        entry = {
            "session_key": key,
            "session": files.session,
            "parts": parts,
            "metadata_file": {"name": METADATA_FILE, "bytes": files.metadata.stat().st_size,
                              "md5": meta_md5},
        }
        (result,) = self.commit(upload, [entry])["sessions"]
        return result


def _code(reply: Response) -> str:
    try:
        return ((reply.json() or {}).get("error") or {}).get("code", "")
    except ValueError:
        return ""


def describe(files: Iterable[Path]) -> list[dict[str, Any]]:
    """``{name, bytes, md5}`` entries for files on disk (for hand-built commits)."""
    out = []
    for path in files:
        md5, size = _md5_and_size(path)
        out.append({"name": path.name, "bytes": size, "md5": md5})
    return out
