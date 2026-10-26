"""The reference rig client against an in-memory Ingest API and bucket.

The fake server is built from the same reference functions a real server uses
(``visio_schema.ingest.auth``), so these tests pin the client's side of the
protocol: what it signs, what it refuses to act on, and when a session counts
as done. The GILabs backend's own end-to-end test drives the server side.
"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from visio_schema.ingest import (
    ManifestEntry,
    content_digest,
    initiate_reply_signature,
    request_signature,
    seal_credentials,
    session_receipt,
)
from visio_schema.ingest.client import (
    HEADER_DATE,
    CommitRejected,
    IngestClient,
    IngestError,
    Response,
    _sigv4_put_headers,
    new_session_key,
)

SECRET = bytes(range(32))
DEVICE = "e54a1de522f7e0f1"
BASE = "https://api.example.com/api/v1/device-upload"
NOW = datetime(2026, 10, 22, 3, 0, tzinfo=timezone.utc)


class FakeApi:
    """A minimal server: one upload, one bucket, receipts per committed session."""

    def __init__(self, *, server_now=NOW, tamper_reply=False, tamper_receipt=False):
        self.server_now = server_now
        self.tamper_reply = tamper_reply
        self.tamper_receipt = tamper_receipt
        self.bucket: dict[str, bytes] = {}
        self.calls: list[str] = []

    def _auth_error(self, method, path, headers, body) -> str:
        """What a server answers (ingest-api.md §4.2): signature first, then the clock."""
        scheme, _, rest = headers.get("Authorization", "").partition(" ")
        fields = dict(f.strip().split("=", 1) for f in rest.split(",") if "=" in f)
        expected = request_signature(
            SECRET, date=headers.get(HEADER_DATE, ""), key_id=fields.get("Credential", ""),
            method=method, path=path, query="", body=body)
        if scheme != "VISIO-HMAC-SHA256" or fields.get("Signature") != expected:
            return "bad_signature"
        sent = datetime.strptime(headers[HEADER_DATE], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc)
        if abs(self.server_now - sent) > timedelta(minutes=15):
            return "clock_skew"
        return ""

    def __call__(self, method, url, headers, body):
        path = urllib.parse.urlsplit(url).path
        self.calls.append(f"{method} {path}")
        if method == "PUT":
            self.bucket[path] = body
            return Response(200, {}, b"")
        date = email.utils.format_datetime(self.server_now, usegmt=True)
        code = self._auth_error(method, path, headers, body)
        if code:
            err = json.dumps({"error": {"code": code, "message": code}}).encode()
            return Response(401, {"date": date}, err)
        request = json.loads(body)
        if path.endswith("/initiate"):
            sealed = seal_credentials(
                SECRET, device_id=DEVICE, upload_id="up_1",
                plaintext=json.dumps({"secret_access_key": "sk", "session_token": "tok"}).encode())
            reply = json.dumps({
                "protocol": "visio-ingest/1", "upload_id": "up_1",
                "expires_at": "2026-10-29T03:00:00Z",
                "storage": {"endpoint_url": "https://r2.example.com", "region": "auto",
                            "bucket": "ingest", "prefix": "v1/acme/up_1/episodes/",
                            "access_key_id": "AKID", "sealed": sealed,
                            "credentials_expire_at": "2026-10-22T04:00:00Z"},
            }).encode()
            signature = initiate_reply_signature(
                SECRET, device_id=DEVICE, request_date=headers[HEADER_DATE], reply_body=reply)
            if self.tamper_reply:
                signature = "0" * 64
            return Response(200, {"date": date, "x-visio-signature": signature}, reply)
        results = []
        for entry in request["sessions"]:
            folder = f"/ingest/v1/acme/up_1/episodes/{entry['session_key']}/"
            missing = [p["name"] for p in [*entry["parts"], entry["metadata_file"]]
                       if f"{folder}{p['name']}" not in self.bucket]
            if missing:
                results.append({"session_key": entry["session_key"], "state": "incomplete",
                                "missing": missing, "mismatched": []})
                continue
            digest = content_digest([
                ManifestEntry(p["name"], p["bytes"], p["md5"]) for p in entry["parts"]])
            receipt = session_receipt(SECRET, device_id=DEVICE, upload_id="up_1",
                                      session_key=entry["session_key"], content_digest=digest)
            if self.tamper_receipt:
                receipt = "f" * 64
            results.append({"session_key": entry["session_key"], "state": "committed",
                            "content_digest": digest, "disposition": "accepted",
                            "receipt": receipt})
        body = json.dumps({"upload": {"upload_id": "up_1", "state": "open"},
                           "sessions": results}).encode()
        return Response(200, {"date": date}, body)


def _client(api, *, clock=lambda: NOW):
    return IngestClient(BASE, key_id="dk_1", secret=SECRET, device_id=DEVICE,
                        transport=api, clock=clock)


def _session(tmp_path: Path) -> Path:
    folder = tmp_path / "session_00042-1786166461"
    folder.mkdir()
    (folder / "ego_0000.mcap").write_bytes(b"\x89MCAP0\r\n" + b"a" * 100)
    (folder / "ego_0001.mcap").write_bytes(b"\x89MCAP0\r\n" + b"b" * 50)
    (folder / "session.json").write_text(json.dumps({"start_time_unix": 1786166461}))
    return folder


def test_a_session_goes_up_and_comes_back_committed(tmp_path):
    api = FakeApi()
    client = _client(api)
    upload = client.initiate(device_kind="ego")
    assert upload.credentials == {"secret_access_key": "sk", "session_token": "tok"}
    result = client.upload_session(upload, _session(tmp_path), session_key="8f1c" * 8)
    assert result["state"] == "committed"
    # Parts first, session.json last, then the commit.
    puts = [c for c in api.calls if c.startswith("PUT")]
    assert puts[-1].endswith("/session.json")
    assert api.calls[-1].endswith("/commit")
    assert api.bucket["/ingest/v1/acme/up_1/episodes/" + "8f1c" * 8 + "/ego_0000.mcap"]


def test_the_rig_corrects_its_clock_from_date_and_re_signs_once(tmp_path):
    api = FakeApi(server_now=NOW + timedelta(hours=3))
    client = _client(api)  # the rig thinks it is 3 hours earlier
    client.initiate(device_kind="ego")
    assert abs(client.clock_offset - timedelta(hours=3)) < timedelta(seconds=1)
    initiate = "POST /api/v1/device-upload/v1/uploads/initiate"
    assert [c for c in api.calls if "initiate" in c] == [initiate] * 2


def test_an_unsigned_initiate_reply_is_not_acted_on():
    with pytest.raises(CommitRejected):
        _client(FakeApi(tamper_reply=True)).initiate(device_kind="ego")


def test_a_forged_receipt_is_not_a_commit(tmp_path):
    api = FakeApi(tamper_receipt=True)
    client = _client(api)
    upload = client.initiate(device_kind="ego")
    with pytest.raises(CommitRejected):
        client.upload_session(upload, _session(tmp_path))


def test_a_wrong_key_surfaces_the_protocol_code():
    client = IngestClient(BASE, key_id="dk_1", secret=bytes(32), device_id=DEVICE,
                          transport=FakeApi(), clock=lambda: NOW)
    with pytest.raises(IngestError) as exc:
        client.initiate(device_kind="ego")
    assert (exc.value.status, exc.value.code) == (401, "bad_signature")


def test_from_endpoint_needs_the_visio_scheme():
    client = IngestClient.from_endpoint("visio+https://h/api", key_id="k", secret=SECRET,
                                        device_id=DEVICE)
    assert client.base_url == "https://h/api"
    with pytest.raises(ValueError):
        IngestClient.from_endpoint("https://h/api", key_id="k", secret=SECRET, device_id=DEVICE)


def test_session_keys_are_random_hex():
    keys = {new_session_key() for _ in range(50)}
    assert len(keys) == 50 and all(len(k) == 32 and int(k, 16) >= 0 for k in keys)


def test_sigv4_put_matches_botocore():
    """The storage PUT is signed the way every S3-compatible store expects."""
    pytest.importorskip("botocore")
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    body = b"hello rig"
    url = "https://acct.r2.cloudflarestorage.com/ingest/v1/acme/up_1/episodes/k/ego_0000.mcap"
    sha = hashlib.sha256(body).hexdigest()
    md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    request = AWSRequest(method="PUT", url=url, data=body, headers={
        "Content-MD5": md5, "X-Amz-Content-SHA256": sha})
    S3SigV4Auth(Credentials("AKID", "SECRET", "TOKEN"), "s3", "auto").add_auth(request)

    # Sign ours at the instant botocore chose, then the two must agree exactly.
    when = datetime.strptime(request.headers["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc)
    ours = _sigv4_put_headers(
        url=url, region="auto", access_key_id="AKID", secret_access_key="SECRET",
        session_token="TOKEN", payload_sha256=sha, content_md5_b64=md5, when=when)
    assert ours["authorization"] == request.headers["Authorization"]
