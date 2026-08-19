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
newer apps.
"""

from __future__ import annotations

import json
import re

__all__ = [
    "BITRATE_KBPS",
    "DEFAULT_STORAGE_PREFIX",
    "ENDPOINT_TEMPLATES",
    "MAX_BYTES",
    "META_FIELDS",
    "PAYLOAD_TYPE",
    "PLAINTEXT_VERSION",
    "PROVIDERS",
    "RESOLUTION_PX",
    "SEALED_HAS",
    "SEALED_MAX_BYTES",
    "SEALED_VERSION",
    "WARN_BYTES",
    "encode",
    "normalize_storage_prefix",
    "region_from_endpoint",
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
META_MAX_CHARS = 256

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

# The provider -> endpoint mapping. The app has its own in
# src/lib/storage/providers.ts, the device its own in
# src/storage/s3_object.hpp and visio-setup its own in
# src/setup_gui/provision.py, because they are different languages; all four
# conform to docs/protocol/storage-providers.md. One row per cloud: the
# endpoint template the operator's choice fills in, and the pattern that
# reads the region back out of a host. Two parallel tables would be two
# things to keep in step; a host matching no row has no derivable region and
# the operator must type one.
PROVIDERS = {
    "Aliyun OSS": (
        "https://oss-{region}.aliyuncs.com",
        r"^oss-([a-z0-9-]+?)(?:-internal)?\.aliyuncs\.com$",
    ),
    "Tencent COS": (
        "https://cos.{region}.myqcloud.com",
        # The leading label is optional so an already-virtual-hosted COS host
        # (<bucket>-<appid>.cos.<region>.myqcloud.com) resolves like the bare
        # one.
        r"(?:^|\.)cos\.([a-z0-9-]+)\.myqcloud\.com$",
    ),
    "AWS S3": (
        "https://s3.{region}.amazonaws.com",
        r"^s3[.-]([a-z0-9-]+)\.amazonaws\.com$",
    ),
}

ENDPOINT_TEMPLATES = {name: tpl for name, (tpl, _) in PROVIDERS.items()}


def region_from_endpoint(endpoint_url: str) -> str:
    """Region encoded in a well-known OSS/COS/S3 endpoint host, or ''.

    Mirrors regionFromEndpoint in the app's lib/storage/providers.ts.
    """
    m = re.match(r"^https?://([^/]+)", endpoint_url)
    if not m:
        return ""
    host = m.group(1).lower()
    if host == "s3.amazonaws.com":   # legacy global endpoint, no region label
        return "us-east-1"
    for _, pattern in PROVIDERS.values():
        hit = re.search(pattern, host)
        if hit:
            return hit.group(1)
    return ""


def _string(errs: list, where: str, v, required: bool = False,
            max_chars: int = META_MAX_CHARS) -> str:
    if v is None:
        if required:
            errs.append(f"{where}: required")
        return ""
    if not isinstance(v, str):
        errs.append(f"{where}: must be a string")
        return ""
    if required and v == "":
        errs.append(f"{where}: required")
    if len(v) > max_chars:
        errs.append(f"{where}: longer than {max_chars} characters")
    return v


def _int_in_range(errs: list, where: str, v, lo: int, hi: int) -> None:
    if not isinstance(v, int) or isinstance(v, bool):
        errs.append(f"{where}: must be an integer")
        return
    if not lo <= v <= hi:
        errs.append(f"{where}: must be {lo}-{hi}")


def _validate_meta(errs: list, meta) -> None:
    if not isinstance(meta, dict):
        errs.append("meta: must be an object")
        return
    for f in meta:
        if f not in META_FIELDS:
            errs.append(f"meta.{f}: unknown field")
    for f in META_FIELDS:
        _string(errs, f"meta.{f}", meta.get(f))


def _validate_storage(errs: list, storage, version: int) -> None:
    if not isinstance(storage, dict):
        errs.append("storage: must be an object")
        return
    allowed = STORAGE_FIELDS[version]
    for f in storage:
        if f in allowed:
            continue
        # Name the migration explicitly. A v2 payload with a plaintext
        # secret is the exact mistake this version exists to prevent, so it
        # gets its own sentence rather than a generic "unknown field".
        if f == "secret_access_key":
            errs.append(
                f"storage.secret_access_key: not allowed in a v{version} "
                "payload — the secret goes in the sealed envelope"
            )
        else:
            errs.append(f"storage.{f}: unknown field")
    endpoint = _string(errs, "storage.endpoint_url",
                       storage.get("endpoint_url"), required=True)
    if endpoint and not re.match(r"^https?://", endpoint):
        errs.append("storage.endpoint_url: must be an http(s) URL")
    region = _string(errs, "storage.region", storage.get("region"))
    if not region and endpoint and not region_from_endpoint(endpoint):
        errs.append("storage.region: required "
                    "(not derivable from the endpoint)")
    _string(errs, "storage.bucket", storage.get("bucket"), required=True)
    _string(errs, "storage.access_key_id", storage.get("access_key_id"),
            required=True)
    if version == PLAINTEXT_VERSION:
        _string(errs, "storage.secret_access_key",
                storage.get("secret_access_key"))
    _string(errs, "storage.prefix", storage.get("prefix"))


def _validate_resolution(errs: list, res) -> None:
    if not isinstance(res, dict):
        errs.append("resolution: must be an object")
        return
    for f in res:
        if f not in ("width", "height"):
            errs.append(f"resolution.{f}: unknown field")
    _int_in_range(errs, "resolution.width", res.get("width"), *RESOLUTION_PX)
    _int_in_range(errs, "resolution.height", res.get("height"),
                  *RESOLUTION_PX)


def _validate_wifi(errs: list, wifi) -> None:
    if not isinstance(wifi, dict):
        errs.append("wifi: must be an object")
        return
    for f in wifi:
        if f not in ("ssid", "passphrase"):
            errs.append(f"wifi.{f}: unknown field")
    ssid = _string(errs, "wifi.ssid", wifi.get("ssid"), required=True)
    if ssid and len(ssid.encode("utf-8")) > 32:
        errs.append("wifi.ssid: longer than 32 bytes")
    psk = _string(errs, "wifi.passphrase", wifi.get("passphrase"))
    # 802.11 WPA-PSK bounds (firmware sanitizer); empty = open net.
    if psk and not 8 <= len(psk) <= 63:
        errs.append("wifi.passphrase: must be 8-63 characters "
                    "(or empty for an open network)")


def _validate_sealed(errs: list, sealed) -> None:
    if not isinstance(sealed, dict):
        errs.append("sealed: must be an object")
        return
    for f in sealed:
        if f not in SEALED_FIELDS:
            errs.append(f"sealed.{f}: unknown field")
    kid = _string(errs, "sealed.kid", sealed.get("kid"), required=True,
                  max_chars=8)
    if kid and not re.fullmatch(r"[0-9a-f]{8}", kid):
        errs.append("sealed.kid: must be 8 lowercase hex characters")
    # base64url of at most SEALED_MAX_BYTES, so 4/3 of the cap.
    _string(errs, "sealed.b", sealed.get("b"), required=True,
            max_chars=(SEALED_MAX_BYTES * 4 + 2) // 3)
    has = sealed.get("has")
    if not isinstance(has, list) or not all(isinstance(h, str) for h in has):
        errs.append("sealed.has: must be a list of strings")
    elif not has:
        errs.append("sealed.has: empty — the envelope sets nothing")
    else:
        for h in has:
            if h not in SEALED_HAS:
                errs.append(f"sealed.has: unknown entry {h!r} "
                            f"(expected one of {list(SEALED_HAS)})")


def validate(cfg: dict) -> list:
    """All problems with a payload dict, as human-readable strings.

    Empty list = valid. Field/range rules mirror the app's parseSettingsQr
    (settings-payload.ts).
    """
    errs: list = []
    if cfg.get("t") != PAYLOAD_TYPE:
        errs.append(f't: must be "{PAYLOAD_TYPE}"')
    version = cfg.get("v")
    if version not in KNOWN_KEYS:
        errs.append(f"v: must be {PLAINTEXT_VERSION} or {SEALED_VERSION}")
        return errs      # every rule below is version-dependent
    for key in cfg:
        if key not in KNOWN_KEYS[version]:
            errs.append(f"{key}: unknown key (the app would ignore it)")

    if not any(k in cfg for k in KNOWN_KEYS[version] - {"t", "v"}):
        errs.append("no settings sections present")

    if "meta" in cfg:
        _validate_meta(errs, cfg["meta"])
    if "storage" in cfg:
        _validate_storage(errs, cfg["storage"], version)
    if "auto_upload" in cfg and not isinstance(cfg["auto_upload"], bool):
        errs.append("auto_upload: must be a boolean")
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
