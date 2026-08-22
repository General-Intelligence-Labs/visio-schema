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
    FALLBACK_PROVIDER,
    META_FIELDS,
    PAYLOAD_TYPE,
    PLAINTEXT_VERSION,
    PROVIDERS,
    RESOLUTION_PX,
    Provider,
    provider_from_endpoint,
    region_from_endpoint,
    region_must_be_typed,
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


def _region_for(provider: Provider, endpoint: str) -> str:
    """This cloud's region, asked for only when it is the operator's to give.

    `region_must_be_typed` owns that decision — it is what `validate` gates on,
    so asking on any other basis would let this build a config its own
    validator rejects. When it says no, the answer is whatever the host
    encodes: a region for OSS/COS/S3, and '' for the row that signs none.
    """
    if not region_must_be_typed(endpoint):
        return region_from_endpoint(endpoint)
    return _ask(provider.region_prompt, provider.region_default)


def _ask_endpoint_and_region(provider: Provider) -> tuple[str, str]:
    """Ask for the one thing this cloud's HOST names, then its region."""
    host_value = _ask(provider.host_prompt) if provider.host_prompt else ""
    endpoint = provider.endpoint_for(host_value)
    return endpoint, _region_for(provider, endpoint)


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
            provider = PROVIDERS[choice]
            endpoint, region = _ask_endpoint_and_region(provider)
        else:
            endpoint = _ask("endpoint_url (https://...)")
            # A hand-typed endpoint is not provider-less: the device resolves
            # it by host like any other, so ask the rest in whichever row it
            # actually resolved to. `provider_from_endpoint` answers None only
            # for a string that is not an http(s) URL at all — which `validate`
            # refuses later — and prompting the rest of the flow in the
            # fallback row's words beats re-asking after a getpass.
            provider = provider_from_endpoint(endpoint) or FALLBACK_PROVIDER
            region = _region_for(provider, endpoint)
        storage = {
            "endpoint_url": endpoint,
            "region": region,
            "bucket": _ask(provider.bucket_prompt),
            "access_key_id": _ask(provider.key_id_prompt),
            "secret_access_key": getpass.getpass(
                f"  {provider.secret_prompt} (empty = device keeps its "
                "stored secret): "),
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
