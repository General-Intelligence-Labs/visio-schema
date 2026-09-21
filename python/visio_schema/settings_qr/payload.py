"""The settings-QR wire form: what the companion app's scanner parses.

Mirrored by the app's parser at
`visio-companion/src/lib/scan/settings-payload.ts` — keep the two in
lock-step (the app repo pins a fixture generated here).

Two payload versions coexist deliberately:

* **v1** carries `storage.secret_access_key` and `wifi.passphrase` in clear
  JSON. Still generated on request (`--plaintext`), still parsed by every
  app, and still the only thing pre-seal firmware understands.
* **v2** moves the secrets into a `sealed` envelope that only a device can
  open, and makes a plaintext `storage.secret_access_key` a hard validation
  error so nobody can half-migrate. The non-secret fields stay in the clear
  on purpose: the app's confirmation screen is a safety feature, and
  blinding the operator to the bucket they are about to write to would cost
  more than it buys.

Validation here is deliberately STRICTER than the app's on unknown keys — a
hard error is a generator-side lint that catches an operator typo on a
laptop, where the app merely warns so that old QRs keep working against
newer apps. Both read a bare whole number in a text field as its text; the
app, whose JSON.parse cannot tell `101.0` from `101`, reads the former too.
"""

from __future__ import annotations

import enum
import json
import re
from typing import NamedTuple

from .i18n import tr

__all__ = [
    "BITRATE_KBPS",
    "DEFAULT_STORAGE_PREFIX",
    "DEVICE_MAX_SIZE",
    "ENDPOINT_TEMPLATES",
    "FALLBACK_PROVIDER",
    "MAX_BYTES",
    "META_FIELDS",
    "META_MAX_BYTES",
    "PAYLOAD_TYPE",
    "PLAINTEXT_VERSION",
    "PROVIDERS",
    "REGION_PROMPT",
    "RESOLUTION_PX",
    "SEALED_HAS",
    "SEALED_MAX_BYTES",
    "SEALED_VERSION",
    "STORAGE_MAX_BYTES",
    "WARN_BYTES",
    "WIFI_MAX_BYTES",
    "Provider",
    "RegionSource",
    "encode",
    "normalize_bare_numbers",
    "normalize_storage_prefix",
    "provider_from_endpoint",
    "region_from_endpoint",
    "region_must_be_typed",
    "validate",
]

PAYLOAD_TYPE = "visio-settings"
PLAINTEXT_VERSION = 1
SEALED_VERSION = 2

# Firmware ranges (the device re-validates; these match
# visio-embedded's src/camera/setting/{bitrate,resolution}.hpp, mirrored in
# the app's settings-payload.ts).
BITRATE_KBPS = (500, 50_000)
RESOLUTION_PX = (240, 4096)

# QR capacity guidance: warn when scanning gets slow from small prints, hard
# stop well under the ~2.9 KB binary limit of a version-40 code.
WARN_BYTES = 1200
MAX_BYTES = 1800

_COMMON_KEYS = {"t", "v", "meta", "storage", "auto_upload", "bitrate_kbps",
                "resolution", "wifi"}
KNOWN_KEYS = {
    PLAINTEXT_VERSION: _COMMON_KEYS,
    SEALED_VERSION: _COMMON_KEYS | {"sealed"},
}

META_FIELDS = ("task", "location", "message", "capturer")
_STORAGE_COMMON = ("endpoint_url", "region", "bucket", "access_key_id",
                   "prefix")
STORAGE_FIELDS = {
    PLAINTEXT_VERSION: (*_STORAGE_COMMON, "secret_access_key"),
    SEALED_VERSION: _STORAGE_COMMON,
}

SEALED_FIELDS = ("kid", "has", "b")

# Field names inside the sealed body. One definition, used by the producer
# (`sealed_body.py`) and by the `has` allowlist below.
SEAL_STORAGE_SECRET = "sk"
SEAL_RECORDING_KEY = "rk"
SEAL_OLD_RECORDING_KEY = "rko"
SEAL_DEVICES = "dev"
SEAL_EXPIRES = "exp"

# What the `has` routing hint may name: the two settings a sealed body can
# carry. `rko` is proof-of-ownership, not a setting, so it is never listed.
SEALED_HAS = (SEAL_STORAGE_SECRET, SEAL_RECORDING_KEY)

# The device's nanopb decode buffer for a sealed field
# (`proto/nanopb.options`: SetStorage/TestStorage/SetRecordingKey `.sealed`).
# nanopb does not truncate an oversized field — it fails pb_decode, so the
# device discards the whole Command and answers nothing. The generator must
# therefore refuse to print a code it knows no device can apply.
# `tests/test_nanopb_options.py` pins this against the options file.
SEALED_MAX_BYTES = 384

DEFAULT_STORAGE_PREFIX = "recordings/"


# The device's static nanopb decode buffers, in BYTES *including* the NUL
# terminator (proto/nanopb.options). A user-typed value longer than
# max_size-1 bytes overflows: nanopb does not truncate, it fails pb_decode, so
# the device drops the whole Command and answers nothing while the app scanning
# the code waits out its timeout with no reason to show. The generator refuses
# to print a code it knows no device can apply. `tests/test_nanopb_options.py`
# pins these against the options file; the app keeps the same caps in
# settings-payload.ts.
DEVICE_MAX_SIZE = {
    "SetRecordingMeta.task": 64,
    "SetRecordingMeta.location": 128,
    "SetRecordingMeta.message": 256,
    "SetRecordingMeta.capturer": 64,
    "SetStorage.endpoint_url": 128,
    "SetStorage.region": 32,
    "SetStorage.bucket": 64,
    "SetStorage.access_key_id": 64,
    "SetStorage.secret_access_key": 64,
    "SetStorage.prefix": 128,
    "ConnectWifi.ssid": 33,
    "ConnectWifi.passphrase": 64,
}


def _usable_bytes(proto_field: str) -> int:
    """Bytes a field can carry: the buffer, less the NUL nanopb reserves.
    A 64-byte buffer holds 63 bytes of text."""
    return DEVICE_MAX_SIZE[proto_field] - 1


# The device byte limit for each field an operator types into. Meta, storage
# and Wi-Fi are the typed sections; bounding every one of their text fields is
# what turns an over-long value into a scan-time notice instead of a silent
# timeout on the rig.
META_MAX_BYTES = {
    f: _usable_bytes(f"SetRecordingMeta.{f}") for f in META_FIELDS}
STORAGE_MAX_BYTES = {
    f: _usable_bytes(f"SetStorage.{f}")
    for f in STORAGE_FIELDS[PLAINTEXT_VERSION]}
WIFI_MAX_BYTES = {
    "ssid": _usable_bytes("ConnectWifi.ssid"),
    "passphrase": _usable_bytes("ConnectWifi.passphrase")}


# The region ask, shared by `host_prompt` (where the host IS the region) and
# `region_prompt` (where it is asked separately). One literal, because they are
# the same question.
REGION_PROMPT = "region (e.g. cn-hangzhou)"


class RegionSource(enum.Enum):
    """Where a destination's credential-scope region comes from.

    Three states, because two cannot tell the difference between "the host
    does not carry a region, so the operator must supply one" and "this
    cloud does not sign a region at all" — and a generator that confuses
    them either blocks an Azure QR on a field nothing reads, or prints a GCS
    QR whose scope region was never asked for.
    """

    FROM_HOST = "from-host"     # the endpoint host encodes it
    OPERATOR = "operator"       # the host does not; the operator types it
    UNUSED = "unused"           # this cloud signs no region (Azure)


class Provider(NamedTuple):
    """One cloud's endpoint shape, as the operator meets it.

    `endpoint_template` has at most one substitution, named for what it
    actually is — `{region}` where the host encodes a geography, `{account}`
    for Azure, and no slot at all for the single global GCS host — so
    `host_prompt` can ask for that thing by its real name.
    """

    endpoint_template: str
    region_source: RegionSource
    # Detection, per storage-providers.md §2, matched against the host.
    # `None` on the row that is the fallback for every unrecognized host.
    host_pattern: str | None = None
    # Reads the region back out of a host; group 1 is the region. Set on
    # exactly the FROM_HOST rows (`test_settings_qr.py` pins that).
    region_pattern: str | None = None
    # What to ask the operator for the template's slot; None where there is
    # no slot to fill and so nothing to ask.
    host_prompt: str | None = REGION_PROMPT
    # What to ask for the region, and its default, where the host does not
    # already answer it. Never reached on a row that signs no region — see
    # `region_must_be_typed`.
    region_prompt: str = REGION_PROMPT
    region_default: str = ""
    # What THIS cloud's own console calls the three credential fields
    # (docs/protocol/storage-providers.md §1.1). The wire field names are
    # provider-agnostic; the values are copied out of consoles that are not,
    # and prompting every cloud with S3's words is how a Tencent SecretId ends
    # up in the secret field. Defaults are the AWS row's wording, which is also
    # the row every unrecognized endpoint falls through to.
    bucket_prompt: str = "bucket"
    key_id_prompt: str = "access key ID"
    secret_prompt: str = "secret access key"

    def endpoint_for(self, host_value: str) -> str:
        """The endpoint URL for what the operator typed at `host_prompt`.

        Both slot names are offered so one call serves every row; a template
        carries at most one of them, and the unused keyword is ignored.
        """
        return self.endpoint_template.format(
            region=host_value, account=host_value)


# One row per cloud. The app has its own table in
# src/lib/storage/providers.ts, the device its own in
# src/storage/s3_object.hpp, because they are different languages; all
# conform to docs/protocol/storage-providers.md.
PROVIDERS = {
    "Aliyun OSS": Provider(
        "https://oss-{region}.aliyuncs.com",
        RegionSource.FROM_HOST,
        host_pattern=r"\.aliyuncs\.com$",
        region_pattern=r"^oss-([a-z0-9-]+?)(?:-internal)?\.aliyuncs\.com$",
        key_id_prompt="AccessKey ID",
        secret_prompt="AccessKey Secret",
    ),
    "Tencent COS": Provider(
        "https://cos.{region}.myqcloud.com",
        RegionSource.FROM_HOST,
        host_pattern=r"\.myqcloud\.com$",
        # The leading label is optional so an already-virtual-hosted COS host
        # (<bucket>-<appid>.cos.<region>.myqcloud.com) resolves like the bare
        # one.
        region_pattern=r"(?:^|\.)cos\.([a-z0-9-]+)\.myqcloud\.com$",
        # The APPID suffix is part of the name; omitting it is the likeliest
        # COS misconfiguration, and the device answers it with a 404.
        bucket_prompt="bucket (name includes the -APPID suffix)",
        key_id_prompt="SecretId",
        secret_prompt="SecretKey",
    ),
    "AWS S3": Provider(
        "https://s3.{region}.amazonaws.com",
        RegionSource.FROM_HOST,
        # No host_pattern: this row is where every unrecognized host lands
        # (MinIO, R2, B2, Wasabi), per §2.
        region_pattern=r"^s3[.-]([a-z0-9-]+)\.amazonaws\.com$",
    ),
    "Google Cloud Storage": Provider(
        # No slot at all: GCS has ONE global XML-API endpoint, and the
        # bucket's location is not part of it.
        "https://storage.googleapis.com",
        RegionSource.OPERATOR,
        # Anchored BOTH ends — the only exact-host row. The virtual-hosted
        # form (<bucket>.storage.googleapis.com) is a different addressing
        # mode that this row would sign wrongly.
        host_pattern=r"^storage\.googleapis\.com$",
        host_prompt=None,
        # The one row whose region has no source but this question.
        region_prompt='region (bucket location, or "auto")',
        region_default="auto",
        # Not a service-account JSON key: the XML API authenticates with an
        # interoperability HMAC pair, and nothing else works here.
        key_id_prompt="HMAC access key (GOOG1E...)",
        secret_prompt="HMAC secret",
    ),
    "Azure Blob": Provider(
        # The slot is the storage ACCOUNT, not a geography, and it is named
        # for that: Azure signs no region at all.
        "https://{account}.blob.core.windows.net",
        RegionSource.UNUSED,
        host_pattern=r"\.blob\.core\.windows\.net$",
        host_prompt="storage account name",
        bucket_prompt="container",
        # The account name a second time: Azure carries it in the host AND in
        # access_key_id, so the prompt says where it came from rather than
        # leaving the operator hunting for a second, different value.
        key_id_prompt="storage account name (the same one, again)",
        secret_prompt="account key",
    ),
}

ENDPOINT_TEMPLATES = {
    name: p.endpoint_template for name, p in PROVIDERS.items()}

# Compiled once. `None` rather than an unmatchable regex on the rows that have
# no such pattern: a pattern that exists invites someone to "fix" it into one
# that matches, and there is nothing there to match.
_REGION_PATTERNS = tuple(
    re.compile(p.region_pattern) for p in PROVIDERS.values()
    if p.region_pattern is not None)
_HOST_PATTERNS = tuple(
    (re.compile(p.host_pattern), p) for p in PROVIDERS.values()
    if p.host_pattern is not None)
# The row every unrecognized host falls through to, per §2. Public because a
# caller that must end up with SOME row (the QR generator, prompting for a
# hand-typed endpoint) would otherwise name "AWS S3" a second time.
FALLBACK_PROVIDER = PROVIDERS["AWS S3"]


def _host_of(endpoint_url: str) -> str:
    """Lowercased host of an http(s) URL, or '' if it is not one."""
    m = re.match(r"^https?://([^/]+)", endpoint_url)
    return m.group(1).lower() if m else ""


def provider_from_endpoint(endpoint_url: str) -> Provider | None:
    """The cloud an endpoint names, by the host rule in storage-providers.md
    §2, or None if the string is not an http(s) URL at all.

    Anything that parses but matches no row is the fallback (plain path-style
    S3: MinIO, R2, B2, Wasabi), which is what the device does with it.
    """
    host = _host_of(endpoint_url)
    if not host:
        return None
    for pattern, provider in _HOST_PATTERNS:
        if pattern.search(host):
            return provider
    return FALLBACK_PROVIDER


def region_from_endpoint(endpoint_url: str) -> str:
    """Region encoded in a well-known OSS/COS/S3 endpoint host, or ''.

    Mirrors regionFromEndpoint in the app's lib/storage/providers.ts.
    """
    host = _host_of(endpoint_url)
    if not host:
        return ""
    if host == "s3.amazonaws.com":   # legacy global endpoint, no region label
        return "us-east-1"
    for pattern in _REGION_PATTERNS:
        hit = pattern.search(host)
        if hit:
            return hit.group(1)
    return ""


def region_must_be_typed(endpoint_url: str) -> bool:
    """Whether the operator still owes a region for this endpoint.

    False in two different cases that a caller must not conflate: the host
    already encodes one, or the cloud signs none at all (Azure). An
    unrecognized host owes one — the fallback row signs a region like every
    other S3 host, and nothing can read it out of a private endpoint.
    """
    if region_from_endpoint(endpoint_url):
        return False
    provider = provider_from_endpoint(endpoint_url)
    return (provider is None
            or provider.region_source is not RegionSource.UNUSED)


def _err(errs: list, where: str | None, key: str, **params: object) -> None:
    """Append one validation problem, translated to the active language.

    `where` is the WIRE field path (`storage.endpoint_url`) and is never
    translated — an operator matches it against the QR payload and the device
    docs, so it must read identically in every locale. Only the reason is
    prose. This is the split the app's parser draws too (settings-payload.ts).
    """
    reason = tr(key, **params)
    errs.append(f"{where}: {reason}" if where else reason)


def over_byte_limit(value: str, max_bytes: int) -> int | None:
    """`value`'s UTF-8 byte length if it overruns the device's decode buffer,
    else None. See DEVICE_MAX_SIZE for why an overrun is fatal, not truncating.
    One home so the validator and the interactive prompt cannot disagree."""
    nbytes = len(value.encode("utf-8"))
    return nbytes if nbytes > max_bytes else None


def _string(errs: list, where: str, v, *, required: bool = False,
            max_bytes: int | None = None, max_chars: int | None = None) -> str:
    # Every string field bounds a length; a call that sets neither would leave
    # the field silently uncapped, which is the very overflow this guards.
    assert max_bytes is not None or max_chars is not None, (
        f"_string({where!r}) set no length cap")
    if v is None:
        if required:
            _err(errs, where, "required")
        return ""
    if not isinstance(v, str):
        _err(errs, where, "mustBeString")
        return ""
    if required and v == "":
        _err(errs, where, "required")
    if max_chars is not None and len(v) > max_chars:
        _err(errs, where, "tooLongChars", max=max_chars)
    if max_bytes is not None:
        over = over_byte_limit(v, max_bytes)
        if over is not None:
            _err(errs, where, "tooLongBytes", bytes=over, max=max_bytes)
    return v


def _is_whole_number(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)  # bool is an int subclass


def _int_in_range(errs: list, where: str, v, lo: int, hi: int) -> None:
    if not _is_whole_number(v):
        _err(errs, where, "mustBeInteger")
        return
    if not lo <= v <= hi:
        _err(errs, where, "intRange", lo=lo, hi=hi)


def _validate_meta(errs: list, meta) -> None:
    if not isinstance(meta, dict):
        _err(errs, "meta", "mustBeObject")
        return
    for f in meta:
        if f not in META_FIELDS:
            _err(errs, f"meta.{f}", "unknownField")
    for f in META_FIELDS:
        _string(errs, f"meta.{f}", meta.get(f), max_bytes=META_MAX_BYTES[f])


def _validate_storage(errs: list, storage, version: int) -> None:
    if not isinstance(storage, dict):
        _err(errs, "storage", "mustBeObject")
        return
    allowed = STORAGE_FIELDS[version]
    for f in storage:
        if f in allowed:
            continue
        # Name the migration explicitly. A v2 payload with a plaintext
        # secret is the exact mistake this version exists to prevent, so it
        # gets its own sentence rather than a generic "unknown field".
        if f == "secret_access_key":
            _err(errs, "storage.secret_access_key", "secretNotAllowedSealed",
                 version=version)
        else:
            _err(errs, f"storage.{f}", "unknownField")
    endpoint = _string(errs, "storage.endpoint_url",
                       storage.get("endpoint_url"), required=True,
                       max_bytes=STORAGE_MAX_BYTES["endpoint_url"])
    if endpoint and not re.match(r"^https?://", endpoint):
        _err(errs, "storage.endpoint_url", "mustBeHttpUrl")
    region = _string(errs, "storage.region", storage.get("region"),
                     max_bytes=STORAGE_MAX_BYTES["region"])
    if not region and endpoint and region_must_be_typed(endpoint):
        _err(errs, "storage.region", "regionNotDerivable")
    _string(errs, "storage.bucket", storage.get("bucket"), required=True,
            max_bytes=STORAGE_MAX_BYTES["bucket"])
    _string(errs, "storage.access_key_id", storage.get("access_key_id"),
            required=True, max_bytes=STORAGE_MAX_BYTES["access_key_id"])
    if version == PLAINTEXT_VERSION:
        _string(errs, "storage.secret_access_key",
                storage.get("secret_access_key"),
                max_bytes=STORAGE_MAX_BYTES["secret_access_key"])
    _string(errs, "storage.prefix", storage.get("prefix"),
            max_bytes=STORAGE_MAX_BYTES["prefix"])


def _validate_resolution(errs: list, res) -> None:
    if not isinstance(res, dict):
        _err(errs, "resolution", "mustBeObject")
        return
    for f in res:
        if f not in ("width", "height"):
            _err(errs, f"resolution.{f}", "unknownField")
    _int_in_range(errs, "resolution.width", res.get("width"), *RESOLUTION_PX)
    _int_in_range(errs, "resolution.height", res.get("height"),
                  *RESOLUTION_PX)


def _validate_wifi(errs: list, wifi) -> None:
    if not isinstance(wifi, dict):
        _err(errs, "wifi", "mustBeObject")
        return
    for f in wifi:
        if f not in ("ssid", "passphrase"):
            _err(errs, f"wifi.{f}", "unknownField")
    _string(errs, "wifi.ssid", wifi.get("ssid"), required=True,
            max_bytes=WIFI_MAX_BYTES["ssid"])
    psk = _string(errs, "wifi.passphrase", wifi.get("passphrase"),
                  max_bytes=WIFI_MAX_BYTES["passphrase"])
    # 802.11 WPA-PSK bounds (firmware sanitizer); empty = open net. The count
    # is characters, the unit the spec states; the byte cap above is the
    # separate device-buffer rule, and a value can trip either.
    if psk and not 8 <= len(psk) <= 63:
        _err(errs, "wifi.passphrase", "wifiPassphraseLength")


def _validate_sealed(errs: list, sealed) -> None:
    if not isinstance(sealed, dict):
        _err(errs, "sealed", "mustBeObject")
        return
    for f in sealed:
        if f not in SEALED_FIELDS:
            _err(errs, f"sealed.{f}", "unknownField")
    kid = _string(errs, "sealed.kid", sealed.get("kid"), required=True,
                  max_chars=8)
    if kid and not re.fullmatch(r"[0-9a-f]{8}", kid):
        _err(errs, "sealed.kid", "kidFormat")
    # base64url of at most SEALED_MAX_BYTES, so 4/3 of the cap.
    _string(errs, "sealed.b", sealed.get("b"), required=True,
            max_chars=(SEALED_MAX_BYTES * 4 + 2) // 3)
    has = sealed.get("has")
    if not isinstance(has, list) or not all(isinstance(h, str) for h in has):
        _err(errs, "sealed.has", "hasList")
    elif not has:
        _err(errs, "sealed.has", "hasEmpty")
    else:
        for h in has:
            if h not in SEALED_HAS:
                _err(errs, "sealed.has", "hasUnknown",
                     entry=repr(h), expected=list(SEALED_HAS))


def validate(cfg: dict) -> list:
    """All problems with a payload dict, as human-readable strings.

    Empty list = valid. The reasons are translated to the active language
    (`--lang`/`$VISIO_LANG`), while the wire field path in each stays English.
    Field/range rules mirror the app's parseSettingsQr (settings-payload.ts).
    """
    errs: list = []
    if cfg.get("t") != PAYLOAD_TYPE:
        _err(errs, "t", "badType", type=PAYLOAD_TYPE)
    version = cfg.get("v")
    if version not in KNOWN_KEYS:
        _err(errs, "v", "badVersion", plaintext=PLAINTEXT_VERSION,
             sealed=SEALED_VERSION)
        return errs      # every rule below is version-dependent
    for key in cfg:
        if key not in KNOWN_KEYS[version]:
            _err(errs, key, "unknownKey")

    if not any(k in cfg for k in KNOWN_KEYS[version] - {"t", "v"}):
        _err(errs, None, "noSections")

    if "meta" in cfg:
        _validate_meta(errs, cfg["meta"])
    if "storage" in cfg:
        _validate_storage(errs, cfg["storage"], version)
    if "auto_upload" in cfg and not isinstance(cfg["auto_upload"], bool):
        _err(errs, "auto_upload", "mustBeBoolean")
    if "bitrate_kbps" in cfg:
        _int_in_range(errs, "bitrate_kbps", cfg["bitrate_kbps"],
                      *BITRATE_KBPS)
    if "resolution" in cfg:
        _validate_resolution(errs, cfg["resolution"])
    if "wifi" in cfg:
        _validate_wifi(errs, cfg["wifi"])
    if "sealed" in cfg:
        _validate_sealed(errs, cfg["sealed"])
    return errs


def encode(cfg: dict) -> str:
    """The compact wire form — byte-identical to what the app parses."""
    return json.dumps(cfg, separators=(",", ":"), ensure_ascii=False)


def normalize_storage_prefix(cfg: dict) -> None:
    """Default an absent/empty storage prefix to 'recordings/' and make any
    prefix slash-terminated — the key join is prefix+name, so a missing '/'
    would silently glue the prefix onto the serial. Runs BEFORE validate():
    the length limit applies to the normalized wire form (what the app's
    parser sees). Wrong-typed prefixes pass through untouched for
    validate() to reject."""
    storage = cfg.get("storage")
    if not isinstance(storage, dict):
        return
    prefix = storage.get("prefix")
    if prefix is None or prefix == "":
        prefix = DEFAULT_STORAGE_PREFIX
    if isinstance(prefix, str) and not prefix.endswith("/"):
        prefix += "/"
    storage["prefix"] = prefix


_TEXT_SECTIONS = ("meta", "storage", "wifi")


def normalize_bare_numbers(cfg: dict) -> None:
    """A bare whole number in a text field (`"location": 101`) is its digits
    with the quotes lost: rewrite it as text, so the printed code carries text
    for every app version. Runs before validate(); any other wrong type is left
    for it to reject."""
    for section in _TEXT_SECTIONS:
        fields = cfg.get(section)
        if not isinstance(fields, dict):
            continue
        for name, value in fields.items():
            if _is_whole_number(value):
                fields[name] = str(value)
