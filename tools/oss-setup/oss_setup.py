#!/usr/bin/env python3
"""Provision an Aliyun OSS bucket + RAM keys for Visio capture devices.

Give it a region and a bucket name; it creates everything a customer needs to
receive recordings and status reports, then PROVES the keys work by replaying
the exact requests the device, the app and the dashboard make.

Stdlib only — no pip install, no aliyun CLI. Copy this one file and run it.

What it creates (each step idempotent; safe to re-run after a failure):

    1. the bucket, private ACL
    2. RAM user + key A  — used by BOTH the device and the phone app
    3. RAM user + key B  — used by the fleet-status dashboard, read-only
    4. a CORS rule, so the dashboard can read the bucket from a browser
    5. a lifecycle rule expiring the status prefix (default 30 days)
    6. verification with the freshly minted keys

Permissions, and why each one exists — this is the whole contract:

    key A  oss:PutObject      <bucket>/*   device uploads recordings + status
                                           reports; the app uploads in
                                           phone-record mode
           oss:ListObjects    <bucket>     the app's cloud recordings list AND
                              (prefix)     the app's "Test" button (the device
                                           probes with a max-keys=1 list) —
                                           restricted to the recordings prefix

    key B  oss:GetObject      <bucket>/status/*   dashboard reads status JSON
           oss:ListObjects    <bucket> (status/)  dashboard enumerates them

Nobody gets oss:DeleteObject. Overwrite protection is header-side
(x-oss-forbid-overwrite), so it needs no bucket configuration. Key A cannot
read the status subtree; key B cannot read recordings or write anything.

The device and the app share ONE credential — the key typed into the app's
Cloud upload screen is sent to the device and mirrored into the phone — which
is why key A is the union of both rows and not write-only.

Usage:
    # interactive: asks for region and bucket, then does everything
    oss_setup.py

    # non-interactive
    oss_setup.py --region cn-hangzhou --bucket my-recording-bucket

    # show what would happen; signs nothing, needs no credentials
    oss_setup.py --region cn-hangzhou --bucket my-bucket --dry-run

    # also save the settings to a file instead of reading them off the screen
    oss_setup.py --region cn-hangzhou --bucket my-bucket --json out.json

Admin credentials (needed to create buckets and RAM users) come from
ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET, or --ossutil-config with
--profile, or an interactive prompt. They are never written to disk.

SECURITY: the two keys it prints are long-lived secrets in plaintext. Treat
the output like a written-down password, and prefer --json (written 0600) to
copy/pasting them through chat.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import datetime
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from typing import Callable, NamedTuple

# --- Contract with the firmware ---------------------------------------------
# These are not free choices; each mirrors a constant the device relies on.

# The OSS V4 signing flavour. A device picks it from the endpoint host, so a
# custom CNAME domain would be signed as AWS S3 instead and rejected.
OSS_ALGORITHM = "OSS4-HMAC-SHA256"
OSS_KEY_PREFIX = "aliyun_v4"
OSS_TERMINATOR = "aliyun_v4_request"
OSS_SERVICE = "oss"
OSS_DATE_HEADER = "x-oss-date"
OSS_CONTENT_HEADER = "x-oss-content-sha256"
# OSS rejects a real payload hash here with 400 InvalidArgument.
OSS_UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"

# The endpoint form the app's "Aliyun OSS" preset fills in.
OSS_ENDPOINT_TEMPLATE = "https://oss-{region}.aliyuncs.com"

# The device's own built-in default for status reports.
DEFAULT_STATUS_PREFIX = "status/"
# A device with no prefix set uploads to the bucket ROOT, but a root prefix
# would make the ListObjects condition on key A meaningless, so this script
# always sets one explicitly.
DEFAULT_PREFIX = "recordings/"

RAM_ENDPOINT = "ram.aliyuncs.com"
RAM_API_VERSION = "2015-05-01"

# The `storage` object of the settings payload --json writes. This is a closed
# set: the provisioning-QR generator that consumes it rejects an unknown storage
# key outright rather than ignoring it, so adding a field here without adding it
# there turns the file into a hard error. The shape is specified in
# docs/protocol/storage-providers.md.
STORAGE_SETTINGS_FIELDS = ("endpoint_url", "region", "bucket", "access_key_id",
                           "secret_access_key", "prefix")

LIFECYCLE_RULE_ID = "visio-status-expiry"
DEFAULT_STATUS_RETENTION_DAYS = 30
PROBE_KEY = ".visio-setup-check"

# RAM policy attachment is eventually consistent; a brand-new key legitimately
# 403s for a moment, so verification retries before calling a grant broken.
VERIFY_TIMEOUT_S = 30.0
VERIFY_BACKOFF_S = 2.0


class SetupError(Exception):
    """A failure with an operator-actionable message. Never a traceback."""


# --- Transport ---------------------------------------------------------------


class HttpResponse(NamedTuple):
    status: int
    body: bytes


# (method, url, headers, body) -> HttpResponse. Injected so the tests can run
# the entire flow — signing included — with no network.
Transport = Callable[[str, str, dict, bytes | None], HttpResponse]


def urllib_transport(method: str, url: str, headers: dict,
                     body: bytes | None) -> HttpResponse:
    """Send one request, deliberately ignoring the environment's proxy.

    Aliyun endpoints are reached directly. A proxy configured for other
    traffic commonly cannot terminate their TLS, and the failure looks like a
    signing error rather than a routing one. An empty ProxyHandler beats
    filtering os.environ because it also covers a proxy set in a config file.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method=method)
    try:
        with opener.open(req, timeout=60) as r:
            return HttpResponse(r.status, r.read())
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, e.read())
    except urllib.error.URLError as e:
        raise SetupError(f"cannot reach {url}: {e.reason}") from e


# --- Signing helpers ---------------------------------------------------------


def percent_encode(value) -> str:
    """RFC 3986 encoding: unreserved characters only, '~' left intact."""
    return urllib.parse.quote(str(value), safe="~")


def canonical_query(params: dict) -> str:
    """Sorted, encoded query string, used verbatim for signing AND sending.

    Building it once is the point: OSS rebuilds the canonical request from
    what it received, so a query that differs by one character between the
    signature and the URL fails with SignatureDoesNotMatch.

    An EMPTY value is a bare key with no '=' — that is the sub-resource form
    (`?cors`, `?lifecycle`), and it is where OSS parts company with AWS, which
    keeps the trailing '='. Sending `cors=` gets the sub-resource acted on but
    canonicalized back to `cors` on the server, so the signature never matches.
    The firmware signer sidesteps the same trap by omitting empty parameters
    (see s3_object.cpp: list_bucket_query).
    """
    return "&".join(
        percent_encode(k) + (f"={percent_encode(params[k])}"
                             if str(params[k]) != "" else "")
        for k in sorted(params)
    )


def _is_oss_default_header(key: str) -> bool:
    """Headers OSS folds into its canonical request without being told.

    Any OTHER signed header
    (host) must be declared in AdditionalHeaders or OSS rebuilds the canonical
    request without it and rejects the signature.
    """
    return (key in ("content-type", "content-md5")
            or key.startswith("x-oss-"))


def sign_oss_v4(access_key_id: str, access_key_secret: str, region: str,
                method: str, host: str, canonical_uri: str, query: str,
                extra_headers: dict, now: datetime.datetime) -> dict:
    """OSS V4 signature, built exactly the way a device builds it.

    `canonical_uri` is what gets SIGNED — always "/<bucket>/<key>" — while the
    request itself goes to <bucket>.<host>/<key>. On OSS those two differ, and
    getting it wrong is the single easiest way to break uploads.
    """
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")

    signed = {k.lower(): str(v).strip() for k, v in extra_headers.items()
              if _is_oss_default_header(k.lower())}
    signed["host"] = host
    signed[OSS_CONTENT_HEADER] = OSS_UNSIGNED_PAYLOAD
    signed[OSS_DATE_HEADER] = stamp

    canonical_headers = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    additional = ";".join(k for k in sorted(signed)
                          if not _is_oss_default_header(k))

    canonical_request = "\n".join([
        method, canonical_uri, query, canonical_headers,
        additional, OSS_UNSIGNED_PAYLOAD,
    ])
    scope = f"{date}/{region}/{OSS_SERVICE}/{OSS_TERMINATOR}"
    string_to_sign = "\n".join([
        OSS_ALGORITHM, stamp, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    key = (OSS_KEY_PREFIX + access_key_secret).encode()
    for part in (date, region, OSS_SERVICE, OSS_TERMINATOR):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(),
                         hashlib.sha256).hexdigest()

    clause = f",AdditionalHeaders={additional}" if additional else ""
    headers = dict(extra_headers)
    headers[OSS_DATE_HEADER] = stamp
    headers[OSS_CONTENT_HEADER] = OSS_UNSIGNED_PAYLOAD
    headers["Authorization"] = (
        f"{OSS_ALGORITHM} Credential={access_key_id}/{scope}"
        f"{clause},Signature={signature}"
    )
    return headers


# --- OSS client --------------------------------------------------------------


def oss_error_code(body: bytes) -> str:
    """<Error><Code>…</Code></Error> -> the code, or '' if unparseable."""
    try:
        root = ET.fromstring(body.decode("utf-8", "replace"))
    except ET.ParseError:
        return ""
    return (root.findtext("Code") or "") if root.tag == "Error" else ""


def oss_error_message(body: bytes) -> str:
    try:
        root = ET.fromstring(body.decode("utf-8", "replace"))
    except ET.ParseError:
        return body[:200].decode("utf-8", "replace")
    return root.findtext("Message") or ""


class OssClient:
    """Virtual-hosted OSS access for exactly the calls this script makes."""

    def __init__(self, region: str, access_key_id: str,
                 access_key_secret: str, transport: Transport,
                 clock: Callable[[], datetime.datetime] | None = None):
        self.region = region
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.transport = transport
        self.clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc))

    @property
    def endpoint_host(self) -> str:
        return f"oss-{self.region}.aliyuncs.com"

    def request(self, method: str, bucket: str, key: str = "",
                params: dict | None = None, body: bytes | None = None,
                headers: dict | None = None) -> HttpResponse:
        query = canonical_query(params or {})
        host = f"{bucket}.{self.endpoint_host}"
        # Encode ONCE and reuse: the signed path and the requested path must
        # agree character for character, or OSS rebuilds a different
        # canonical request and answers SignatureDoesNotMatch.
        encoded_key = urllib.parse.quote(key)
        # Signed path carries the bucket; the request path does not.
        canonical_uri = f"/{bucket}/{encoded_key}"
        extra = dict(headers or {})
        if body:
            extra["content-md5"] = base64.b64encode(
                hashlib.md5(body).digest()).decode()
        signed = sign_oss_v4(self.access_key_id, self.access_key_secret,
                             self.region, method, host, canonical_uri, query,
                             extra, self.clock())
        url = f"https://{host}/{encoded_key}"
        if query:
            url += "?" + query
        return self.transport(method, url, signed, body)


# --- RAM client --------------------------------------------------------------


class RamClient:
    """Aliyun RPC v1 (HMAC-SHA1) calls against the RAM control plane."""

    def __init__(self, access_key_id: str, access_key_secret: str,
                 transport: Transport,
                 nonce: Callable[[], str] | None = None,
                 clock: Callable[[], datetime.datetime] | None = None):
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.transport = transport
        self.nonce = nonce or (lambda: uuid.uuid4().hex)
        self.clock = clock or (
            lambda: datetime.datetime.now(datetime.timezone.utc))

    def call(self, action: str, **extra) -> dict:
        """Invoke one RAM action. Returns the parsed JSON body.

        Aliyun answers a rejected call with a non-2xx status AND a JSON body
        carrying a machine-readable Code, so the caller can branch on
        'already exists' without string-matching a message.
        """
        params = {
            "Action": action,
            "Version": RAM_API_VERSION,
            "Format": "JSON",
            "AccessKeyId": self.access_key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": self.nonce(),
            "Timestamp": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        params.update({k: v for k, v in extra.items() if v is not None})
        query = canonical_query(params)
        to_sign = f"POST&{percent_encode('/')}&{percent_encode(query)}"
        signature = base64.b64encode(
            hmac.new((self.access_key_secret + "&").encode(),
                     to_sign.encode(), hashlib.sha1).digest()).decode()
        body = (query + "&Signature=" + percent_encode(signature)).encode()
        resp = self.transport(
            "POST", f"https://{RAM_ENDPOINT}/",
            {"Content-Type": "application/x-www-form-urlencoded"}, body)
        try:
            return json.loads(resp.body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise SetupError(
                f"RAM {action}: unparseable reply (HTTP {resp.status}): "
                f"{resp.body[:200]!r}") from None


# --- The permission contract -------------------------------------------------


def device_policy(bucket: str, prefix: str) -> dict:
    """Key A: device + app. See the module docstring for why each entry."""
    return {
        "Version": "1",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["oss:PutObject"],
                "Resource": [f"acs:oss:*:*:{bucket}/*"],
            },
            {
                # The app's cloud recordings list AND the app's Test button
                # (the device probes with a max-keys=1 list), confined to the
                # recordings prefix so this key cannot enumerate the status
                # subtree.
                "Effect": "Allow",
                "Action": ["oss:ListObjects"],
                "Resource": [f"acs:oss:*:*:{bucket}"],
                "Condition": {
                    "StringLike": {"oss:Prefix": [f"{prefix}*", prefix]}
                },
            },
        ],
    }


def dashboard_policy(bucket: str, status_prefix: str) -> dict:
    """Key B: fleet-status dashboard. Read-only, status subtree only."""
    return {
        "Version": "1",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["oss:GetObject"],
                "Resource": [f"acs:oss:*:*:{bucket}/{status_prefix}*"],
            },
            {
                "Effect": "Allow",
                "Action": ["oss:ListObjects"],
                "Resource": [f"acs:oss:*:*:{bucket}"],
                "Condition": {
                    "StringLike": {
                        "oss:Prefix": [f"{status_prefix}*", status_prefix]
                    }
                },
            },
        ],
    }


def cors_document() -> bytes:
    """Byte-identical to what the fleet-status dashboard documents.

    AllowedOrigin is the literal string "null" because a double-clicked local
    file reports that as its origin. It grants nothing on its own — every
    request is still signed and the bucket stays private.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<CORSConfiguration>\n"
        "  <CORSRule>\n"
        "    <AllowedOrigin>null</AllowedOrigin>\n"
        "    <AllowedMethod>GET</AllowedMethod>\n"
        "    <AllowedMethod>HEAD</AllowedMethod>\n"
        "    <AllowedHeader>*</AllowedHeader>\n"
        "    <ExposeHeader>ETag</ExposeHeader>\n"
        "    <ExposeHeader>Date</ExposeHeader>\n"
        "  </CORSRule>\n"
        "</CORSConfiguration>\n"
    ).encode()


def lifecycle_document(existing: bytes | None, status_prefix: str,
                       days: int) -> bytes:
    """Our status-expiry rule merged into whatever rules already exist.

    A customer's own rules are preserved; only a previous rule with our own ID
    is replaced, so re-running never accumulates duplicates and never destroys
    a retention policy someone else set up.
    """
    out = ET.Element("LifecycleConfiguration")
    if existing:
        try:
            root = ET.fromstring(existing.decode("utf-8", "replace"))
        except ET.ParseError:
            root = None
        if root is not None and root.tag == "LifecycleConfiguration":
            for rule in root.findall("Rule"):
                if (rule.findtext("ID") or "").strip() != LIFECYCLE_RULE_ID:
                    out.append(rule)
    rule = ET.SubElement(out, "Rule")
    ET.SubElement(rule, "ID").text = LIFECYCLE_RULE_ID
    ET.SubElement(rule, "Prefix").text = status_prefix
    ET.SubElement(rule, "Status").text = "Enabled"
    ET.SubElement(ET.SubElement(rule, "Expiration"), "Days").text = str(days)
    return ET.tostring(out, encoding="utf-8")


# --- Input normalisation and the guards --------------------------------------


def normalise_region(value: str) -> str:
    """Accept a region, an endpoint URL, or a host — reject the near-misses.

    Every branch here is a real failure clients hit. The messages name the
    fix, because 'invalid region' teaches nobody anything.
    """
    text = (value or "").strip().lower().rstrip("/")
    if not text:
        raise SetupError("region is required (for example: cn-hangzhou)")

    if "://" in text or ".aliyuncs.com" in text or "." in text:
        host = text.split("://", 1)[-1].split("/", 1)[0]
        if not host.endswith(".aliyuncs.com"):
            raise SetupError(
                f"{host!r} is not an *.aliyuncs.com endpoint. A custom CNAME "
                "domain cannot be used: the device picks OSS signing from the "
                "endpoint host, and anything else is signed as AWS S3 and "
                "rejected. Use the region, e.g. cn-hangzhou.")
        if "-internal." in host:
            raise SetupError(
                f"{host!r} is an internal endpoint, reachable only from "
                "inside Aliyun. A device on Wi-Fi cannot resolve it. Use the "
                "public region, e.g. cn-hangzhou.")
        m = re.fullmatch(r"oss-([a-z0-9-]+)\.aliyuncs\.com", host)
        if not m:
            raise SetupError(
                f"{host!r} looks like a Bucket 域名 (it already contains the "
                "bucket). Use the region instead, e.g. cn-hangzhou — the "
                "bucket is prepended automatically, so a bucket domain would "
                "produce <bucket>.<bucket>.oss-… and fail to resolve.")
        text = m.group(1)

    text = text.removeprefix("oss-")
    if not re.fullmatch(r"[a-z0-9-]+", text):
        raise SetupError(f"{value!r} is not a valid region, e.g. cn-hangzhou")
    return text


def validate_bucket(value: str) -> str:
    """OSS naming rules, checked before we spend a round-trip on them."""
    name = (value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", name):
        raise SetupError(
            f"{value!r} is not a valid bucket name: 3-63 characters, "
            "lowercase letters, digits and hyphens, not starting or ending "
            "with a hyphen.")
    return name


def normalise_prefix(value: str, fallback: str) -> str:
    """A prefix is a directory; enforce the trailing slash exactly once."""
    text = (value or "").strip().lstrip("/")
    if not text:
        text = fallback
    return text if text.endswith("/") else text + "/"


def ram_name(template: str, bucket: str, limit: int) -> str:
    """RAM names have their own charset and length cap, unlike buckets."""
    name = template.format(bucket=bucket)
    return re.sub(r"[^A-Za-z0-9._-]", "-", name)[:limit]


# --- Provisioning steps ------------------------------------------------------


def ensure_bucket(oss: OssClient, bucket: str, report) -> None:
    resp = oss.request("PUT", bucket, headers={"x-oss-acl": "private"})
    if 200 <= resp.status < 300:
        report(f"bucket {bucket}: created (private)")
        return
    code = oss_error_code(resp.body)
    if code in ("BucketAlreadyOwnedByYou",):
        report(f"bucket {bucket}: already exists")
        return
    if code == "BucketAlreadyExists":
        # OSS uses this code both for "yours" and "someone else's". A HEAD
        # with the same admin key settles which, and the two need very
        # different advice.
        head = oss.request("HEAD", bucket)
        if 200 <= head.status < 300:
            report(f"bucket {bucket}: already exists")
            return
        raise SetupError(
            f"bucket name {bucket!r} is taken by another Aliyun account "
            "(OSS bucket names are globally unique). Pick another name.")
    raise SetupError(
        f"could not create bucket {bucket}: HTTP {resp.status} {code} "
        f"{oss_error_message(resp.body)}")


def _ram_code(reply: dict) -> str:
    return str(reply.get("Code") or "")


def ensure_ram_user(ram: RamClient, user: str, report) -> None:
    reply = ram.call("CreateUser", UserName=user)
    code = _ram_code(reply)
    if not code:
        report(f"RAM user {user}: created")
    elif code == "EntityAlreadyExists.User":
        report(f"RAM user {user}: already exists")
    else:
        raise SetupError(f"could not create RAM user {user}: {code} "
                         f"{reply.get('Message', '')}")


def ensure_policy(ram: RamClient, policy: str, document: dict,
                  report) -> None:
    """Create the policy, or add a new default version if it already exists.

    Updating rather than skipping matters: a bucket provisioned by an older
    run of this script would otherwise keep a stale grant forever, and the
    verification below would fail with no clue why.
    """
    body = json.dumps(document, separators=(",", ":"), sort_keys=False)
    reply = ram.call("CreatePolicy", PolicyName=policy, PolicyDocument=body,
                     Description="Visio capture device access")
    code = _ram_code(reply)
    if not code:
        report(f"policy {policy}: created")
        return
    if code != "EntityAlreadyExists.Policy":
        raise SetupError(f"could not create policy {policy}: {code} "
                         f"{reply.get('Message', '')}")

    reply = ram.call("CreatePolicyVersion", PolicyName=policy,
                     PolicyDocument=body, SetAsDefault="true")
    code = _ram_code(reply)
    if not code:
        report(f"policy {policy}: updated to a new default version")
        return
    if code == "LimitExceeded.Policy.Version":
        _prune_policy_versions(ram, policy)
        reply = ram.call("CreatePolicyVersion", PolicyName=policy,
                         PolicyDocument=body, SetAsDefault="true")
        code = _ram_code(reply)
        if not code:
            report(f"policy {policy}: updated (pruned an old version)")
            return
    raise SetupError(f"could not update policy {policy}: {code} "
                     f"{reply.get('Message', '')}")


def _prune_policy_versions(ram: RamClient, policy: str) -> None:
    """Aliyun caps a policy at 5 versions; drop the oldest non-default one."""
    reply = ram.call("ListPolicyVersions", PolicyName=policy,
                     PolicyType="Custom")
    versions = (reply.get("PolicyVersions", {}) or {}).get("PolicyVersion", [])
    stale = [v for v in versions if not v.get("IsDefaultVersion")]
    if not stale:
        raise SetupError(
            f"policy {policy} has no removable versions; delete one in the "
            "RAM console and re-run.")
    oldest = min(stale, key=lambda v: v.get("CreateDate", ""))
    ram.call("DeletePolicyVersion", PolicyName=policy,
             VersionId=oldest.get("VersionId"))


def ensure_attached(ram: RamClient, user: str, policy: str, report) -> None:
    reply = ram.call("AttachPolicyToUser", PolicyName=policy,
                     PolicyType="Custom", UserName=user)
    code = _ram_code(reply)
    if not code:
        report(f"policy {policy}: attached to {user}")
    elif code == "EntityAlreadyExists.User.Policy":
        report(f"policy {policy}: already attached to {user}")
    else:
        raise SetupError(f"could not attach {policy} to {user}: {code} "
                         f"{reply.get('Message', '')}")


def create_access_key(ram: RamClient, user: str, report) -> tuple[str, str]:
    """Mint a fresh key. An existing secret can never be read back."""
    reply = ram.call("CreateAccessKey", UserName=user)
    code = _ram_code(reply)
    if code == "LimitExceeded.User.AccessKey":
        raise SetupError(
            f"RAM user {user} already has the maximum number of access keys, "
            "and an existing secret cannot be read back. Delete an unused "
            "key for that user in the RAM console, then re-run.")
    if code:
        raise SetupError(f"could not create an access key for {user}: {code} "
                         f"{reply.get('Message', '')}")
    key = reply.get("AccessKey", {})
    if not key.get("AccessKeyId") or not key.get("AccessKeySecret"):
        raise SetupError(f"RAM did not return an access key for {user}")
    report(f"access key for {user}: created ({key['AccessKeyId']})")
    return key["AccessKeyId"], key["AccessKeySecret"]


def put_cors(oss: OssClient, bucket: str, report) -> None:
    body = cors_document()
    resp = oss.request("PUT", bucket, params={"cors": ""}, body=body,
                       headers={"content-type": "application/xml"})
    if not 200 <= resp.status < 300:
        raise SetupError(
            f"could not set the CORS rule: HTTP {resp.status} "
            f"{oss_error_code(resp.body)} {oss_error_message(resp.body)}")
    report("CORS rule: set (origin 'null', GET/HEAD)")


def put_lifecycle(oss: OssClient, bucket: str, status_prefix: str, days: int,
                  report) -> None:
    current = oss.request("GET", bucket, params={"lifecycle": ""})
    existing = current.body if 200 <= current.status < 300 else None
    body = lifecycle_document(existing, status_prefix, days)
    resp = oss.request("PUT", bucket, params={"lifecycle": ""}, body=body,
                       headers={"content-type": "application/xml"})
    if not 200 <= resp.status < 300:
        raise SetupError(
            f"could not set the lifecycle rule: HTTP {resp.status} "
            f"{oss_error_code(resp.body)} {oss_error_message(resp.body)}")
    report(f"lifecycle rule: {status_prefix} expires after {days} days")


# --- Verification ------------------------------------------------------------


class Check(NamedTuple):
    label: str
    ok: bool
    detail: str


def _failure_detail(resp: HttpResponse, grant: str) -> str:
    """Why a check failed: the service's own Code first, then the grant.

    Naming only the grant mis-sends the operator — a 404 (bucket does not
    exist) and a 403 SignatureDoesNotMatch (secret typo'd) are not missing
    grants, and both used to be reported as one.
    """
    code = oss_error_code(resp.body)
    return (f"HTTP {resp.status} {code}".rstrip()
            + f" — expected {grant} to be in effect")


def _retry_until_ok(attempt: Callable[[], HttpResponse],
                    accept: Callable[[int], bool],
                    sleep: Callable[[float], None],
                    now: Callable[[], float]) -> HttpResponse:
    """Retry past the eventual consistency of a freshly attached policy."""
    deadline = now() + VERIFY_TIMEOUT_S
    while True:
        resp = attempt()
        if accept(resp.status) or now() >= deadline:
            return resp
        sleep(VERIFY_BACKOFF_S)


def verify(region: str, bucket: str, prefix: str, status_prefix: str,
           device_key: tuple[str, str], dashboard_key: tuple[str, str],
           transport: Transport, sleep=time.sleep,
           now=time.monotonic) -> list[Check]:
    """Replay what each consumer actually does, using the new keys.

    This is the part that makes the script worth running. Every check below
    corresponds to a row of the permission table; a client hitting a failure
    in the app would only see "Access denied (HTTP 403)".
    """
    device = OssClient(region, *device_key, transport)
    dashboard = OssClient(region, *dashboard_key, transport)
    ok = lambda s: 200 <= s < 300                              # noqa: E731
    listing = {"list-type": "2", "max-keys": "1"}
    checks = []

    # Exactly what the app's "Test" button makes the device do — the same
    # max-keys=1 list under the recordings prefix the app's cloud recordings
    # list makes, so one grant serves both.
    resp = _retry_until_ok(
        lambda: device.request("GET", bucket,
                               params={**listing, "prefix": prefix}),
        ok, sleep, now)
    checks.append(Check(
        "device key: the app's Test button + cloud recordings list",
        ok(resp.status),
        "" if ok(resp.status) else _failure_detail(resp, "oss:ListObjects")))

    # Uploading, which the Test button never proves.
    resp = _retry_until_ok(
        lambda: device.request(
            "PUT", bucket, key=prefix + PROBE_KEY, body=b"visio-setup\n",
            headers={"x-oss-forbid-overwrite": "true"}),
        # 409 is the overwrite-protection collision, which a device also
        # treats as success.
        lambda s: ok(s) or s == 409, sleep, now)
    good = ok(resp.status) or resp.status == 409
    checks.append(Check(
        "device key: upload a recording (PutObject)", good,
        "" if good else _failure_detail(resp, "oss:PutObject")))

    resp = _retry_until_ok(
        lambda: dashboard.request("GET", bucket,
                                  params={**listing, "prefix": status_prefix}),
        ok, sleep, now)
    checks.append(Check(
        "dashboard key: read the status reports", ok(resp.status),
        "" if ok(resp.status) else
        _failure_detail(resp, "the dashboard's read grants")))

    return checks


# --- Credentials -------------------------------------------------------------


def admin_credentials(ossutil_config: str | None,
                      profile: str) -> tuple[str, str]:
    """Env, then an ossutil config, then a prompt. Never persisted."""
    key = os.environ.get("ALIYUN_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("ALIYUN_ACCESS_KEY_SECRET", "").strip()
    if key and secret:
        return key, secret

    if ossutil_config:
        parser = configparser.ConfigParser()
        if not parser.read(os.path.expanduser(ossutil_config)):
            raise SetupError(f"cannot read {ossutil_config}")
        # ossutil writes named profiles as "[profile name]" and keeps the
        # unnamed default in "[Credentials]"; a hand-rolled file often uses
        # the bare name. Try each spelling before giving up.
        candidates = [profile, f"profile {profile}"]
        if profile == "default":
            candidates.append("Credentials")
        section = next((s for s in candidates if parser.has_section(s)), None)
        if section is None:
            raise SetupError(
                f"{ossutil_config} has no profile {profile!r} "
                f"(found: {', '.join(parser.sections()) or 'none'})")
        key = parser.get(section, "accessKeyID", fallback="").strip()
        secret = parser.get(section, "accessKeySecret", fallback="").strip()
        if key and secret:
            return key, secret
        raise SetupError(f"profile {profile!r} has no access key")

    sys.stderr.write(
        "Aliyun admin credentials (needed to create the bucket and the RAM\n"
        "users). They are used for this run only and never stored.\n")
    key = input("  AccessKey ID: ").strip()
    secret = getpass.getpass("  AccessKey Secret: ").strip()
    if not key or not secret:
        raise SetupError("admin credentials are required")
    return key, secret


# --- Reporting ---------------------------------------------------------------


def render_summary(region: str, bucket: str, prefix: str, status_prefix: str,
                   device_key: tuple[str, str],
                   dashboard_key: tuple[str, str]) -> str:
    endpoint = OSS_ENDPOINT_TEMPLATE.format(region=region)
    return "\n".join([
        "",
        "=" * 68,
        "Enter these in the app, under Device settings -> Cloud upload:",
        "",
        f"  Endpoint URL        {endpoint}",
        f"  Region              {region}",
        f"  Bucket              {bucket}",
        f"  Access key ID       {device_key[0]}",
        f"  Secret access key   {device_key[1]}",
        f"  Prefix              {prefix}",
        f"  Status prefix       {status_prefix}",
        "",
        "Fleet-status dashboard (read-only — do NOT put this in the app):",
        "",
        f"  Access key ID       {dashboard_key[0]}",
        f"  Secret access key   {dashboard_key[1]}",
        "",
        "Both secrets are shown once. Store them like passwords.",
        "=" * 68,
    ])


def write_settings_json(path: str, region: str, bucket: str, prefix: str,
                        device_key: tuple[str, str]) -> None:
    """The device settings, in the shape the provisioning tools accept.

    Saving them to a 0600 file beats copy/pasting a secret through chat, and
    it lets them be imported rather than retyped.
    """
    payload = {
        "t": "visio-settings",
        "v": 1,
        "storage": {
            "endpoint_url": OSS_ENDPOINT_TEMPLATE.format(region=region),
            "region": region,
            "bucket": bucket,
            "access_key_id": device_key[0],
            "secret_access_key": device_key[1],
            "prefix": prefix,
        },
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def render_plan(region: str, bucket: str, prefix: str, status_prefix: str,
                days: int, device_user: str, dashboard_user: str) -> str:
    endpoint = OSS_ENDPOINT_TEMPLATE.format(region=region)
    return "\n".join([
        f"Would provision, in region {region} ({endpoint}):",
        f"  bucket           {bucket} (private)",
        f"  RAM user         {device_user}     -> key A (device + app)",
        f"  RAM user         {dashboard_user}  -> key B (dashboard)",
        f"  CORS             origin 'null', GET/HEAD",
        f"  lifecycle        {status_prefix} expires after {days} days",
        "",
        "Key A policy:",
        json.dumps(device_policy(bucket, prefix), indent=2),
        "",
        "Key B policy:",
        json.dumps(dashboard_policy(bucket, status_prefix), indent=2),
    ])


# --- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--region", metavar="REGION",
                   help="Aliyun region, e.g. cn-hangzhou (prompted if unset)")
    p.add_argument("--bucket", metavar="NAME",
                   help="bucket to create or reuse (prompted if unset)")
    p.add_argument("--prefix", metavar="PATH", default=DEFAULT_PREFIX,
                   help="recordings prefix (default: %(default)s)")
    p.add_argument("--status-prefix", metavar="PATH",
                   default=DEFAULT_STATUS_PREFIX,
                   help="status-report prefix (default: %(default)s)")
    p.add_argument("--status-retention-days", type=int,
                   default=DEFAULT_STATUS_RETENTION_DAYS, metavar="N",
                   help="expire status reports after N days "
                        "(default: %(default)s)")
    p.add_argument("--json", metavar="FILE",
                   help="also write the device settings as JSON, mode 0600")
    p.add_argument("--ossutil-config", metavar="FILE",
                   default=os.environ.get("OSSUTIL_CONFIG"),
                   help="read admin credentials from an ossutil config")
    p.add_argument("--profile", default=os.environ.get("OSS_PROFILE",
                                                       "default"),
                   help="profile within --ossutil-config "
                        "(default: %(default)s)")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be done; sign and send nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        region = normalise_region(
            args.region or input("Region (e.g. cn-hangzhou): "))
        bucket = validate_bucket(
            args.bucket or input("Bucket name: "))
        prefix = normalise_prefix(args.prefix, DEFAULT_PREFIX)
        status_prefix = normalise_prefix(args.status_prefix,
                                         DEFAULT_STATUS_PREFIX)
        if args.status_retention_days < 1:
            raise SetupError("--status-retention-days must be at least 1")

        device_user = ram_name("visio-{bucket}-device", bucket, 64)
        dashboard_user = ram_name("visio-{bucket}-dashboard", bucket, 64)
        device_pol = ram_name("VisioDevice-{bucket}", bucket, 128)
        dashboard_pol = ram_name("VisioDashboard-{bucket}", bucket, 128)

        if args.dry_run:
            print(render_plan(region, bucket, prefix, status_prefix,
                              args.status_retention_days, device_user,
                              dashboard_user))
            return 0

        admin = admin_credentials(args.ossutil_config, args.profile)
        transport = urllib_transport
        oss = OssClient(region, *admin, transport)
        ram = RamClient(*admin, transport)
        report = lambda msg: print(f"  {msg}")             # noqa: E731

        print(f"==> Bucket, in {region}")
        ensure_bucket(oss, bucket, report)

        print("==> Key A — device + app")
        ensure_ram_user(ram, device_user, report)
        ensure_policy(ram, device_pol, device_policy(bucket, prefix), report)
        ensure_attached(ram, device_user, device_pol, report)
        device_key = create_access_key(ram, device_user, report)

        print("==> Key B — fleet-status dashboard (read-only)")
        ensure_ram_user(ram, dashboard_user, report)
        ensure_policy(ram, dashboard_pol,
                      dashboard_policy(bucket, status_prefix), report)
        ensure_attached(ram, dashboard_user, dashboard_pol, report)
        dashboard_key = create_access_key(ram, dashboard_user, report)

        print("==> Bucket configuration")
        put_cors(oss, bucket, report)
        put_lifecycle(oss, bucket, status_prefix,
                      args.status_retention_days, report)

        print("==> Verifying the keys really work")
        checks = verify(region, bucket, prefix, status_prefix, device_key,
                        dashboard_key, transport)
        for check in checks:
            mark = "ok  " if check.ok else "FAIL"
            report(f"[{mark}] {check.label}"
                   + (f"\n         {check.detail}" if check.detail else ""))

        print(render_summary(region, bucket, prefix, status_prefix,
                             device_key, dashboard_key))
        if args.json:
            write_settings_json(args.json, region, bucket, prefix, device_key)
            print(f"Device settings written to {args.json} (mode 0600). "
                  "It contains the secret — keep it like a password.")

        if not all(c.ok for c in checks):
            print("\nSome checks failed — the keys above are NOT ready to "
                  "use. The detail line names the missing grant.",
                  file=sys.stderr)
            return 1
        return 0
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
