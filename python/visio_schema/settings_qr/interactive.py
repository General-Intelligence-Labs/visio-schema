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
    ENDPOINT_TEMPLATES,
    META_FIELDS,
    PAYLOAD_TYPE,
    PLAINTEXT_VERSION,
    RESOLUTION_PX,
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
        providers = [*ENDPOINT_TEMPLATES, "custom"]
        choice = _ask(f"provider {providers}", providers[0])
        region = _ask("region (e.g. cn-hangzhou)")
        if choice in ENDPOINT_TEMPLATES:
            endpoint = ENDPOINT_TEMPLATES[choice].format(region=region)
        else:
            endpoint = _ask("endpoint_url (https://...)")
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
