"""Section-by-section prompts for building a settings payload by hand.

Produces the same v1-shaped dict a `--config` file would; the caller decides
whether to seal it. Secrets are read with `getpass` so they never land in a
shell history or a scrollback buffer.
"""

from __future__ import annotations

import getpass
import sys

from .payload import (
    BITRATE_KBPS,
    DEFAULT_STORAGE_PREFIX,
    META_FIELDS,
    PAYLOAD_TYPE,
    PLAINTEXT_VERSION,
    PROVIDERS,
    RESOLUTION_PX,
    Provider,
    RegionSource,
    region_from_endpoint,
)

__all__ = ["interactive"]


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {prompt}{suffix}: ").strip()
    return val or default


def _ask_yn(prompt: str) -> bool:
    return input(f"{prompt} [y/N]: ").strip().lower() in ("y", "yes")


def _ask_int(prompt: str, default: str, lo: int, hi: int) -> int:
    # Re-prompt in place — bailing out of interactive() on a typo would
    # throw away everything already typed, including getpass'd secrets.
    while True:
        raw = _ask(prompt, default)
        try:
            val = int(raw)
        except ValueError:
            print(f"  not a number: {raw!r}", file=sys.stderr)
            continue
        if lo <= val <= hi:
            return val
        print(f"  out of range ({lo}-{hi}): {val}", file=sys.stderr)


def _ask_endpoint_and_region(provider: Provider) -> tuple[str, str]:
    """Ask for the one thing this cloud's HOST names, then its region.

    The two are asked separately because they are not always the same
    answer: an OSS/COS/S3 host encodes its region, so the single value serves
    both; GCS's host encodes none, so the region is its own question; and
    Azure's host names a storage ACCOUNT while signing no region at all, so
    asking for one would invite an operator to type a geography where the
    account belongs and get a QR for a host that does not exist.
    """
    host_value = _ask(provider.host_prompt) if provider.host_prompt else ""
    endpoint = provider.endpoint_for(host_value)
    if provider.region_source is RegionSource.FROM_HOST:
        return endpoint, region_from_endpoint(endpoint)
    if provider.region_source is RegionSource.OPERATOR:
        return endpoint, _ask('region (bucket location, or "auto")', "auto")
    return endpoint, ""


def interactive() -> dict:
    """Prompt for each section; empty answers omit the field/section.

    Always returns the v1 shape (secret in `storage.secret_access_key`) —
    sealing is the caller's step, so there is exactly one place that decides
    where a secret ends up.
    """
    cfg = {"t": PAYLOAD_TYPE, "v": PLAINTEXT_VERSION}
    print("Empty answer skips a field. Ctrl-C aborts.\n", file=sys.stderr)

    if _ask_yn("Configure capture metadata (task/location/...)?"):
        meta = {f: _ask(f) for f in META_FIELDS}
        # Skipped fields are omitted; the CLI prints the will-be-cleared note.
        cfg["meta"] = {k: v for k, v in meta.items() if v}

    if _ask_yn("Configure cloud upload (OSS/S3)?"):
        providers = [*PROVIDERS, "custom"]
        choice = _ask(f"provider {providers}", providers[0])
        if choice in PROVIDERS:
            endpoint, region = _ask_endpoint_and_region(PROVIDERS[choice])
        else:
            endpoint = _ask("endpoint_url (https://...)")
            region = _ask("region (e.g. cn-hangzhou)")
        storage = {
            "endpoint_url": endpoint,
            "region": region,
            "bucket": _ask("bucket"),
            "access_key_id": _ask("access_key_id"),
            "secret_access_key": getpass.getpass(
                "  secret_access_key (empty = device keeps its stored "
                "secret): "),
            "prefix": _ask("prefix", DEFAULT_STORAGE_PREFIX),
        }
        cfg["storage"] = {k: v for k, v in storage.items() if v}
        cfg["auto_upload"] = _ask_yn("Enable auto-upload?")

    if _ask_yn("Set video bitrate?"):
        cfg["bitrate_kbps"] = _ask_int("bitrate_kbps", "8000", *BITRATE_KBPS)

    if _ask_yn("Set camera resolution?"):
        cfg["resolution"] = {
            "width": _ask_int("width", "1920", *RESOLUTION_PX),
            "height": _ask_int("height", "1080", *RESOLUTION_PX),
        }

    if _ask_yn("Configure device Wi-Fi?"):
        wifi = {"ssid": _ask("ssid")}
        psk = getpass.getpass("  passphrase (empty = open network): ")
        if psk:
            wifi["passphrase"] = psk
        cfg["wifi"] = wifi

    return cfg
