"""Host unit tests for oss_setup.py — signing, the permission contract, the
input guards, and the idempotent provisioning flow.

Everything runs offline through an injected transport, so the whole flow
(signing included) is exercised with no Aliyun account and no network.

The signing tests deliberately mirror the firmware's own SigV4/OSS-V4 suite:
this script must produce the SAME OSS V4 construction the device does, or a key
it provisions would work here and fail on the device.
"""
import datetime
import importlib.util
import itertools
import json
import os
import stat

import pytest

_SCRIPT = os.path.join(os.path.dirname(__file__), "oss_setup.py")
_spec = importlib.util.spec_from_file_location("oss_setup", _SCRIPT)
oss_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oss_setup)

NOW = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
BUCKET = "my-bucket"
REGION = "cn-hangzhou"
HOST = f"{BUCKET}.oss-{REGION}.aliyuncs.com"


def sign(**over):
    kwargs = dict(
        access_key_id="LTAI-AKID", access_key_secret="osssecret",
        region=REGION, method="PUT", host=HOST,
        canonical_uri=f"/{BUCKET}/k.json", query="", extra_headers={},
        now=NOW)
    kwargs.update(over)
    return oss_setup.sign_oss_v4(**kwargs)


def auth(**over):
    return sign(**over)["Authorization"]


class FakeTransport:
    """Records every request; replies from the first matching rule."""

    def __init__(self, rules=None, default=None):
        self.rules = list(rules or [])
        self.default = default or oss_setup.HttpResponse(200, b"")
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body})
        for match, resp in self.rules:
            if match(method, url, body):
                return resp
        return self.default


def ram_reply(**fields):
    return oss_setup.HttpResponse(200, json.dumps(fields).encode())


def oss_error(status, code):
    body = f"<?xml version='1.0'?><Error><Code>{code}</Code>" \
           f"<Message>nope</Message></Error>".encode()
    return oss_setup.HttpResponse(status, body)


def silent(_msg):
    pass


# --- OSS V4 signing: must match the firmware -------------------------------


def test_authorization_matches_the_firmware_structure():
    # The exact prefix tests/test_sigv4.cpp pins for the device signer.
    assert auth().startswith(
        "OSS4-HMAC-SHA256 Credential=LTAI-AKID/20240102/cn-hangzhou/oss/"
        "aliyun_v4_request,AdditionalHeaders=host,Signature=")


def test_signature_is_stable():
    # Golden pin: a refactor that changes the signed bytes fails here rather
    # than at a customer's bucket.
    assert auth(extra_headers={"content-type": "application/json"}) == (
        "OSS4-HMAC-SHA256 Credential=LTAI-AKID/20240102/cn-hangzhou/oss/"
        "aliyun_v4_request,AdditionalHeaders=host,Signature="
        "c788eaa2fbbf26ef4f9f8ac191edcfc18ba9b3fde74496b2586f3eae1f23534e")


def test_oss_only_ever_sends_unsigned_payload():
    # A real payload hash is rejected by OSS with 400 InvalidArgument.
    headers = sign(extra_headers={"content-type": "text/xml"})
    assert headers[oss_setup.OSS_CONTENT_HEADER] == "UNSIGNED-PAYLOAD"
    assert headers[oss_setup.OSS_DATE_HEADER] == "20240102T030405Z"


def test_never_emits_the_aws_signed_headers_clause():
    assert "SignedHeaders=" not in auth()


@pytest.mark.parametrize("header", ["content-type", "content-md5",
                                    "x-oss-forbid-overwrite", "x-oss-acl"])
def test_default_signed_headers_change_the_signature(header):
    # OSS folds these into ITS canonical request whether we sign them or not,
    # so sending one unsigned guarantees SignatureDoesNotMatch.
    a = auth(extra_headers={header: "one"})
    b = auth(extra_headers={header: "two"})
    assert a != b


def test_default_signed_headers_need_no_declaration():
    # They are defaults; only non-default signed headers (host) are declared.
    assert "AdditionalHeaders=host," in auth(
        extra_headers={"content-type": "application/json"})


def test_content_length_is_not_signed():
    # The boundary of the rule: OSS does not fold content-length.
    assert auth(extra_headers={"content-length": "1"}) == \
        auth(extra_headers={"content-length": "999"})


def test_region_is_part_of_the_credential_scope():
    # A wrong region is the classic silent 403; it must change the signature.
    assert auth(region="cn-beijing") != auth(region="cn-hangzhou")


def test_client_signs_the_bucket_path_but_does_not_request_it():
    # On OSS the signed URI keeps /<bucket>/<key> while the request is
    # virtual-hosted. Getting this wrong is the easiest way to break uploads.
    transport = FakeTransport()
    client = oss_setup.OssClient(REGION, "LTAI-AKID", "osssecret", transport,
                                 clock=lambda: NOW)
    client.request("PUT", BUCKET, key="recordings/x.mcap")

    call = transport.calls[0]
    assert call["url"] == f"https://{HOST}/recordings/x.mcap"
    assert call["headers"]["Authorization"] == auth(
        method="PUT", canonical_uri=f"/{BUCKET}/recordings/x.mcap")


def test_key_is_encoded_identically_in_the_signature_and_the_url():
    # Signed path and requested path must agree character for character, or
    # OSS rebuilds a different canonical request and rejects the signature.
    transport = FakeTransport()
    client = oss_setup.OssClient(REGION, "LTAI-AKID", "osssecret", transport,
                                 clock=lambda: NOW)
    client.request("PUT", BUCKET, key="recordings/a b+c.mcap")

    call = transport.calls[0]
    assert call["url"] == f"https://{HOST}/recordings/a%20b%2Bc.mcap"
    assert call["headers"]["Authorization"] == auth(
        method="PUT", canonical_uri=f"/{BUCKET}/recordings/a%20b%2Bc.mcap")


def test_query_is_signed_and_sent_identically():
    transport = FakeTransport()
    client = oss_setup.OssClient(REGION, "LTAI-AKID", "osssecret", transport,
                                 clock=lambda: NOW)
    client.request("GET", BUCKET, params={"prefix": "status/",
                                          "list-type": "2"})
    url = transport.calls[0]["url"]
    # Sorted, percent-encoded, and the URL carries exactly what was signed.
    assert url.endswith("?list-type=2&prefix=status%2F")


@pytest.mark.parametrize("sub", ["cors", "lifecycle"])
def test_sub_resource_is_a_bare_key_with_no_equals(sub):
    # OSS canonicalizes an empty value as the bare key; AWS keeps the '='.
    # Signing `cors=` is a live-only 403 SignatureDoesNotMatch — the request
    # reaches the right sub-resource, so nothing offline notices.
    transport = FakeTransport()
    client = oss_setup.OssClient(REGION, "LTAI-AKID", "osssecret", transport,
                                 clock=lambda: NOW)
    client.request("GET", BUCKET, params={sub: ""})

    call = transport.calls[0]
    assert call["url"] == f"https://{HOST}/?{sub}"
    assert call["headers"]["Authorization"] == auth(
        method="GET", canonical_uri=f"/{BUCKET}/", query=sub)


# --- RAM RPC v1 -------------------------------------------------------------


def test_ram_request_body_and_signature():
    transport = FakeTransport(default=ram_reply())
    ram = oss_setup.RamClient("LTAI-AKID", "ramsecret", transport,
                              nonce=lambda: "n0nce", clock=lambda: NOW)
    ram.call("CreateUser", UserName="visio-x-device")

    body = transport.calls[0]["body"].decode()
    assert body == (
        "AccessKeyId=LTAI-AKID&Action=CreateUser&Format=JSON"
        "&SignatureMethod=HMAC-SHA1&SignatureNonce=n0nce"
        "&SignatureVersion=1.0&Timestamp=2024-01-02T03%3A04%3A05Z"
        "&UserName=visio-x-device&Version=2015-05-01"
        "&Signature=Wa9qfRXBY7PsTQ%2BQRIxZ2ENX99M%3D")
    assert transport.calls[0]["url"] == "https://ram.aliyuncs.com/"


def test_ram_omits_unset_optional_parameters():
    transport = FakeTransport(default=ram_reply())
    ram = oss_setup.RamClient("k", "s", transport, nonce=lambda: "n",
                              clock=lambda: NOW)
    ram.call("CreatePolicy", PolicyName="P", Description=None)
    assert "Description" not in transport.calls[0]["body"].decode()


# --- The permission contract ------------------------------------------------
# These are the spec. A silent widening of any grant fails here.


def test_device_policy_is_exactly_the_documented_grant():
    assert oss_setup.device_policy(BUCKET, "recordings/") == {
        "Version": "1",
        "Statement": [
            {"Effect": "Allow", "Action": ["oss:PutObject"],
             "Resource": ["acs:oss:*:*:my-bucket/*"]},
            {"Effect": "Allow", "Action": ["oss:ListObjects"],
             "Resource": ["acs:oss:*:*:my-bucket"],
             "Condition": {"StringLike": {
                 "oss:Prefix": ["recordings/*", "recordings/"]}}},
        ],
    }


def test_dashboard_policy_is_exactly_the_documented_grant():
    assert oss_setup.dashboard_policy(BUCKET, "status/") == {
        "Version": "1",
        "Statement": [
            {"Effect": "Allow", "Action": ["oss:GetObject"],
             "Resource": ["acs:oss:*:*:my-bucket/status/*"]},
            {"Effect": "Allow", "Action": ["oss:ListObjects"],
             "Resource": ["acs:oss:*:*:my-bucket"],
             "Condition": {"StringLike": {
                 "oss:Prefix": ["status/*", "status/"]}}},
        ],
    }


def test_the_test_buttons_exact_prefix_is_itself_granted():
    # The device probes with prefix="recordings/" EXACTLY, so the
    # wildcard-less entry is load-bearing — it is NOT a duplicate of
    # "recordings/*". Dropping it 403s the Test button on every device.
    listing = next(s for s in oss_setup.device_policy(BUCKET, "recordings/")
                   ["Statement"] if s["Action"] == ["oss:ListObjects"])
    assert "recordings/" in listing["Condition"]["StringLike"]["oss:Prefix"]


def test_no_policy_grants_delete():
    both = json.dumps([oss_setup.device_policy(BUCKET, "recordings/"),
                       oss_setup.dashboard_policy(BUCKET, "status/")])
    assert "Delete" not in both


def test_device_key_cannot_read_the_status_subtree():
    # Key A lists only the recordings prefix; the status subtree is key B's.
    policy = oss_setup.device_policy(BUCKET, "recordings/")
    listing = next(s for s in policy["Statement"]
                   if s["Action"] == ["oss:ListObjects"])
    prefixes = listing["Condition"]["StringLike"]["oss:Prefix"]
    assert all(not p.startswith("status/") for p in prefixes)
    assert "oss:GetObject" not in json.dumps(policy)


# --- Input guards -----------------------------------------------------------


@pytest.mark.parametrize("given,expected", [
    ("cn-hangzhou", "cn-hangzhou"),
    ("  CN-Hangzhou ", "cn-hangzhou"),
    ("oss-cn-hangzhou", "cn-hangzhou"),
    ("https://oss-cn-hangzhou.aliyuncs.com", "cn-hangzhou"),
    ("https://oss-cn-hangzhou.aliyuncs.com/", "cn-hangzhou"),
    ("oss-cn-hangzhou.aliyuncs.com", "cn-hangzhou"),
])
def test_region_accepted_forms(given, expected):
    assert oss_setup.normalise_region(given) == expected


def test_region_rejects_a_bucket_domain():
    # The console shows this next to the endpoint, and the firmware prepends
    # the bucket — so it would resolve <bucket>.<bucket>.oss-… and fail.
    with pytest.raises(oss_setup.SetupError, match="Bucket 域名"):
        oss_setup.normalise_region(
            "https://my-bucket.oss-cn-hangzhou.aliyuncs.com")


def test_region_rejects_an_internal_endpoint():
    with pytest.raises(oss_setup.SetupError, match="internal endpoint"):
        oss_setup.normalise_region("oss-cn-hangzhou-internal.aliyuncs.com")


def test_region_rejects_a_custom_cname():
    # The device picks OSS signing from the host; anything else is signed as
    # AWS SigV4 and 403s.
    with pytest.raises(oss_setup.SetupError, match="custom CNAME"):
        oss_setup.normalise_region("https://oss.example.com")


def test_region_rejects_empty():
    with pytest.raises(oss_setup.SetupError, match="required"):
        oss_setup.normalise_region("  ")


@pytest.mark.parametrize("bad", ["ab", "-leading", "trailing-", "dot.name",
                                 "has_underscore", "a" * 64])
def test_bucket_names_are_validated(bad):
    with pytest.raises(oss_setup.SetupError):
        oss_setup.validate_bucket(bad)


def test_bucket_name_is_lowercased():
    assert oss_setup.validate_bucket("  My-Bucket ") == "my-bucket"


@pytest.mark.parametrize("given,expected", [
    ("", "recordings/"), ("captures", "captures/"), ("captures/", "captures/"),
    ("/captures", "captures/"),
])
def test_prefix_always_ends_in_one_slash(given, expected):
    assert oss_setup.normalise_prefix(given, "recordings/") == expected


def test_ram_names_are_charset_and_length_capped():
    name = oss_setup.ram_name("visio-{bucket}-device", "a" * 80, 64)
    assert len(name) == 64
    assert oss_setup.ram_name("visio-{bucket}-device", "a.b", 64) == \
        "visio-a.b-device"


# --- Provisioning: idempotency ---------------------------------------------


def test_ensure_bucket_creates_it_privately():
    transport = FakeTransport()
    oss = oss_setup.OssClient(REGION, "k", "s", transport, clock=lambda: NOW)
    oss_setup.ensure_bucket(oss, BUCKET, silent)
    assert transport.calls[0]["method"] == "PUT"
    assert transport.calls[0]["headers"]["x-oss-acl"] == "private"


def test_ensure_bucket_is_idempotent_when_already_ours():
    transport = FakeTransport(default=oss_error(409,
                                                "BucketAlreadyOwnedByYou"))
    oss = oss_setup.OssClient(REGION, "k", "s", transport, clock=lambda: NOW)
    oss_setup.ensure_bucket(oss, BUCKET, silent)  # must not raise


def test_ensure_bucket_accepts_an_existing_bucket_we_can_head():
    # OSS reuses BucketAlreadyExists for "yours" too; a HEAD settles it.
    rules = [(lambda m, u, b: m == "PUT",
              oss_error(409, "BucketAlreadyExists")),
             (lambda m, u, b: m == "HEAD",
              oss_setup.HttpResponse(200, b""))]
    oss = oss_setup.OssClient(REGION, "k", "s", FakeTransport(rules),
                              clock=lambda: NOW)
    oss_setup.ensure_bucket(oss, BUCKET, silent)


def test_ensure_bucket_reports_a_name_taken_by_someone_else():
    rules = [(lambda m, u, b: m == "PUT",
              oss_error(409, "BucketAlreadyExists")),
             (lambda m, u, b: m == "HEAD",
              oss_setup.HttpResponse(403, b""))]
    oss = oss_setup.OssClient(REGION, "k", "s", FakeTransport(rules),
                              clock=lambda: NOW)
    with pytest.raises(oss_setup.SetupError, match="globally unique"):
        oss_setup.ensure_bucket(oss, BUCKET, silent)


def test_ensure_ram_user_tolerates_an_existing_user():
    transport = FakeTransport(
        default=ram_reply(Code="EntityAlreadyExists.User"))
    ram = oss_setup.RamClient("k", "s", transport, nonce=lambda: "n",
                              clock=lambda: NOW)
    oss_setup.ensure_ram_user(ram, "visio-x-device", silent)


def test_ensure_policy_updates_an_existing_policy():
    # A stale grant from an older run must not survive silently.
    seen = []

    def transport(method, url, headers, body):
        action = dict(p.split("=", 1) for p in body.decode().split("&")
                      if "=" in p)["Action"]
        seen.append(action)
        if action == "CreatePolicy":
            return ram_reply(Code="EntityAlreadyExists.Policy")
        return ram_reply()

    ram = oss_setup.RamClient("k", "s", transport, nonce=lambda: "n",
                              clock=lambda: NOW)
    oss_setup.ensure_policy(ram, "VisioDevice-x",
                            oss_setup.device_policy(BUCKET, "recordings/"),
                            silent)
    assert seen == ["CreatePolicy", "CreatePolicyVersion"]


def test_ensure_attached_tolerates_an_existing_attachment():
    transport = FakeTransport(
        default=ram_reply(Code="EntityAlreadyExists.User.Policy"))
    ram = oss_setup.RamClient("k", "s", transport, nonce=lambda: "n",
                              clock=lambda: NOW)
    oss_setup.ensure_attached(ram, "u", "p", silent)


def test_create_access_key_explains_the_two_key_limit():
    # The secret of an existing key can never be read back, so this needs a
    # human decision rather than a silent retry.
    transport = FakeTransport(
        default=ram_reply(Code="LimitExceeded.User.AccessKey"))
    ram = oss_setup.RamClient("k", "s", transport, nonce=lambda: "n",
                              clock=lambda: NOW)
    with pytest.raises(oss_setup.SetupError, match="RAM console"):
        oss_setup.create_access_key(ram, "visio-x-device", silent)


def test_create_access_key_returns_the_pair():
    transport = FakeTransport(default=ram_reply(
        AccessKey={"AccessKeyId": "LTAI-new", "AccessKeySecret": "shh"}))
    ram = oss_setup.RamClient("k", "s", transport, nonce=lambda: "n",
                              clock=lambda: NOW)
    assert oss_setup.create_access_key(ram, "u", silent) == ("LTAI-new", "shh")


# --- Bucket configuration ---------------------------------------------------


def test_cors_document_matches_what_the_dashboard_documents():
    body = oss_setup.cors_document().decode()
    assert "<AllowedOrigin>null</AllowedOrigin>" in body
    assert "<AllowedMethod>GET</AllowedMethod>" in body
    assert "<AllowedMethod>HEAD</AllowedMethod>" in body
    assert "<ExposeHeader>ETag</ExposeHeader>" in body


def test_lifecycle_merge_preserves_a_customers_own_rules():
    existing = (b"<LifecycleConfiguration>"
                b"<Rule><ID>keep-me</ID><Prefix>recordings/</Prefix>"
                b"<Status>Enabled</Status>"
                b"<Expiration><Days>3650</Days></Expiration></Rule>"
                b"</LifecycleConfiguration>")
    merged = oss_setup.lifecycle_document(existing, "status/", 30).decode()
    assert "keep-me" in merged
    assert "3650" in merged
    assert oss_setup.LIFECYCLE_RULE_ID in merged


def test_lifecycle_merge_replaces_our_own_previous_rule():
    first = oss_setup.lifecycle_document(None, "status/", 30)
    second = oss_setup.lifecycle_document(first, "status/", 7).decode()
    assert second.count(oss_setup.LIFECYCLE_RULE_ID) == 1
    assert "<Days>7</Days>" in second
    assert "<Days>30</Days>" not in second


def test_lifecycle_survives_an_unparseable_existing_document():
    merged = oss_setup.lifecycle_document(b"<not xml", "status/", 30).decode()
    assert oss_setup.LIFECYCLE_RULE_ID in merged


def test_put_lifecycle_raises_with_the_service_message():
    rules = [(lambda m, u, b: m == "PUT", oss_error(403, "AccessDenied"))]
    oss = oss_setup.OssClient(REGION, "k", "s", FakeTransport(rules),
                              clock=lambda: NOW)
    with pytest.raises(oss_setup.SetupError, match="AccessDenied"):
        oss_setup.put_lifecycle(oss, BUCKET, "status/", 30, silent)


# --- Verification -----------------------------------------------------------


def fake_clock():
    return itertools.count(0, 100).__next__


def test_verification_passes_when_every_grant_works():
    checks = oss_setup.verify(
        REGION, BUCKET, "recordings/", "status/", ("a", "b"), ("c", "d"),
        FakeTransport(), sleep=lambda _: None, now=fake_clock())
    assert [c.ok for c in checks] == [True, True, True]
    assert len(checks) == 3


def test_device_probe_lists_one_key_under_the_recordings_prefix():
    # Byte-identical to what the firmware signs — tests/test_s3_object.cpp
    # pins this same string for list_bucket_query("recordings"). If the two
    # drift, a key this script calls good 403s the device's Test button.
    #
    # Only the non-empty form is cross-checked, and that is deliberate: the
    # firmware OMITS the parameter entirely when no prefix is set (AWS and OSS
    # canonicalize an empty value differently), while normalise_prefix here
    # always falls back to recordings/, so verify() cannot produce that case.
    # It is not drift — a device left with no prefix lists the bucket ROOT,
    # which the policy above confines away, and that 403 is the one the
    # firmware's own message explains.
    transport = FakeTransport()
    oss_setup.verify(REGION, BUCKET, "recordings/", "status/",
                     ("DEVKEY", "s1"), ("DASHKEY", "s2"), transport,
                     sleep=lambda _: None, now=fake_clock())
    probe = transport.calls[0]
    assert probe["method"] == "GET"
    assert probe["url"] == (
        f"https://{HOST}/?list-type=2&max-keys=1&prefix=recordings%2F")
    # The status subtree is key B's; key A must never probe it.
    assert "status" not in probe["url"]


def test_probe_upload_treats_forbid_overwrite_conflict_as_success():
    # The firmware treats 409 the same way; a re-run must not look broken.
    rules = [(lambda m, u, b: m == "PUT",
              oss_error(409, "FileAlreadyExists"))]
    checks = oss_setup.verify(
        REGION, BUCKET, "recordings/", "status/", ("a", "b"), ("c", "d"),
        FakeTransport(rules), sleep=lambda _: None, now=fake_clock())
    assert all(c.ok for c in checks)


def test_verification_names_the_missing_grant():
    # Fail only the device key's list (the dashboard lists the status prefix).
    rules = [(lambda m, u, b: m == "GET" and "recordings" in u,
              oss_error(403, "AccessDenied"))]
    checks = oss_setup.verify(
        REGION, BUCKET, "recordings/", "status/", ("a", "b"), ("c", "d"),
        FakeTransport(rules), sleep=lambda _: None, now=fake_clock())
    probe = checks[0]
    assert not probe.ok
    assert "ListObjects" in probe.detail
    assert all(c.ok for c in checks[1:])


def test_verification_uses_each_key_for_its_own_role():
    transport = FakeTransport()
    oss_setup.verify(REGION, BUCKET, "recordings/", "status/",
                     ("DEVKEY", "s1"), ("DASHKEY", "s2"), transport,
                     sleep=lambda _: None, now=fake_clock())
    auths = [c["headers"]["Authorization"] for c in transport.calls]
    assert all("DEVKEY" in a for a in auths[:2])
    assert "DASHKEY" in auths[2]


# --- Admin credentials ------------------------------------------------------


def test_env_credentials_win(monkeypatch):
    monkeypatch.setenv("ALIYUN_ACCESS_KEY_ID", "LTAI-env")
    monkeypatch.setenv("ALIYUN_ACCESS_KEY_SECRET", "envsecret")
    assert oss_setup.admin_credentials(None, "default") == \
        ("LTAI-env", "envsecret")


def _ossutil_config(tmp_path, section):
    path = tmp_path / "ossutil.conf"
    path.write_text(f"[{section}]\nendpoint=oss-cn-hangzhou.aliyuncs.com\n"
                    "accessKeyID=LTAI-file\naccessKeySecret=filesecret\n")
    return str(path)


def test_named_ossutil_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("ALIYUN_ACCESS_KEY_ID", raising=False)
    cfg = _ossutil_config(tmp_path, "profile staging")
    assert oss_setup.admin_credentials(cfg, "staging") == \
        ("LTAI-file", "filesecret")


def test_ossutil_default_lives_in_the_credentials_section(tmp_path,
                                                          monkeypatch):
    # ossutil keeps the unnamed default in [Credentials], not [default].
    monkeypatch.delenv("ALIYUN_ACCESS_KEY_ID", raising=False)
    cfg = _ossutil_config(tmp_path, "Credentials")
    assert oss_setup.admin_credentials(cfg, "default") == \
        ("LTAI-file", "filesecret")


def test_missing_ossutil_profile_lists_what_is_there(tmp_path, monkeypatch):
    monkeypatch.delenv("ALIYUN_ACCESS_KEY_ID", raising=False)
    cfg = _ossutil_config(tmp_path, "profile staging")
    with pytest.raises(oss_setup.SetupError, match="profile staging"):
        oss_setup.admin_credentials(cfg, "prod")


def test_unreadable_ossutil_config_is_reported(monkeypatch):
    monkeypatch.delenv("ALIYUN_ACCESS_KEY_ID", raising=False)
    with pytest.raises(oss_setup.SetupError, match="cannot read"):
        oss_setup.admin_credentials("/nonexistent/ossutil.conf", "default")


# --- Output -----------------------------------------------------------------


def test_settings_json_is_qr_compatible_and_private(tmp_path):
    out = tmp_path / "settings.json"
    oss_setup.write_settings_json(str(out), REGION, BUCKET, "recordings/",
                                  ("LTAI-x", "secret"))
    payload = json.loads(out.read_text())
    assert payload["t"] == "visio-settings" and payload["v"] == 1
    assert payload["storage"] == {
        "endpoint_url": "https://oss-cn-hangzhou.aliyuncs.com",
        "region": "cn-hangzhou",
        "bucket": "my-bucket",
        "access_key_id": "LTAI-x",
        "secret_access_key": "secret",
        "prefix": "recordings/",
    }
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_settings_json_emits_exactly_the_declared_storage_fields(tmp_path):
    # The consumer is the provisioning-QR generator, which lives in the firmware
    # repo and rejects an unknown storage key outright. It cannot be imported
    # from here, so each side pins this same closed set against its own half of
    # the contract; a field added on one side alone breaks that side's suite.
    out = tmp_path / "settings.json"
    oss_setup.write_settings_json(str(out), REGION, BUCKET, "recordings/",
                                  ("LTAI-x", "secret"))
    emitted = json.loads(out.read_text())["storage"]
    assert tuple(emitted) == oss_setup.STORAGE_SETTINGS_FIELDS
    assert set(oss_setup.STORAGE_SETTINGS_FIELDS) == {
        "endpoint_url", "region", "bucket", "access_key_id",
        "secret_access_key", "prefix"}


def test_summary_shows_both_keys_and_warns_about_the_dashboard_key():
    text = oss_setup.render_summary(REGION, BUCKET, "recordings/", "status/",
                                    ("LTAI-dev", "s1"), ("LTAI-dash", "s2"))
    assert "LTAI-dev" in text and "LTAI-dash" in text
    assert "do NOT put this in the app" in text


# --- CLI --------------------------------------------------------------------


def test_dry_run_needs_no_credentials_and_sends_nothing(capsys, monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("--dry-run must not touch the network")

    monkeypatch.setattr(oss_setup, "urllib_transport", explode)
    monkeypatch.delenv("ALIYUN_ACCESS_KEY_ID", raising=False)

    rc = oss_setup.main(["--region", REGION, "--bucket", BUCKET, "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "oss:PutObject" in out
    assert "oss:ListObjects" in out
    # The whole point of the trimmed contract: put + list, nothing else.
    assert "oss:GetBucketInfo" not in out


def test_a_bad_region_exits_with_an_actionable_message(capsys):
    rc = oss_setup.main(["--region", "https://oss.example.com",
                         "--bucket", BUCKET, "--dry-run"])
    assert rc == 2
    assert "custom CNAME" in capsys.readouterr().err


def test_retention_must_be_positive(capsys):
    rc = oss_setup.main(["--region", REGION, "--bucket", BUCKET,
                         "--status-retention-days", "0", "--dry-run"])
    assert rc == 2
    assert "at least 1" in capsys.readouterr().err
