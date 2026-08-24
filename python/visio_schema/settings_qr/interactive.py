"""Section-by-section prompts for building a settings payload by hand.

Produces the same v1-shaped dict a `--config` file would; the caller decides
whether to seal it. Secrets are read with `getpass` so they never land in a
shell history or a scrollback buffer.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from .i18n import LANGUAGES, detect_language, set_language, tr
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

__all__ = ["interactive", "interactive_recording_key"]


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


#: What each language calls itself. Never translated — a speaker recognises
#: their own language's name, and a "Chinese" they cannot read is no help.
_LANGUAGE_NAMES = {"en": "English", "zh": "中文"}


def ask_language() -> str:
    """Pick the prompt language, before anything else is asked.

    Bilingual by necessity: this is the one question asked before we know what
    the operator reads, so it has to be answerable either way. The detected
    locale is the default, so Enter is right for the common case and nobody
    who does not care has to think about it.
    """
    current = detect_language()
    print("\nLanguage / 语言", file=sys.stderr)
    for i, code in enumerate(LANGUAGES, 1):
        marker = "  <-" if code == current else ""
        print(f"    {i}) {_LANGUAGE_NAMES[code]}{marker}", file=sys.stderr)
    default = str(LANGUAGES.index(current) + 1)
    while True:
        raw = _ask("choose / 请选择", default)
        if raw.isdigit() and 1 <= int(raw) <= len(LANGUAGES):
            return set_language(LANGUAGES[int(raw) - 1])
        low = raw.lower()
        if low in LANGUAGES:
            return set_language(low)
        for code, name in _LANGUAGE_NAMES.items():
            if name.lower() == low:
                return set_language(code)
        print(f"    ? {raw!r}", file=sys.stderr)


def _ask_provider() -> str | None:
    """Pick a cloud. Returns its key, or None for a hand-typed endpoint.

    Numbered, because the keys are display names with spaces and capitals
    ("Tencent COS") and the previous exact-match test routed a perfectly
    reasonable "tencent" into the custom-endpoint branch — which then asks for
    a URL the operator does not have and cannot guess. Silently answering a
    different question than the one the user thought they answered is the
    worst failure a prompt can have, so this re-asks instead.
    """
    names = [*PROVIDERS]
    print("  " + tr("cloudProvider"), file=sys.stderr)
    for i, name in enumerate(names, 1):
        print(f"    {i}) {name}", file=sys.stderr)
    print(f"    {len(names) + 1}) " + tr("customEndpoint"), file=sys.stderr)
    while True:
        raw = _ask(tr("choose"), "1")
        if raw.isdigit():
            i = int(raw)
            if 1 <= i <= len(names):
                return names[i - 1]
            if i == len(names) + 1:
                return None
            print("  " + tr("notAChoice", value=raw), file=sys.stderr)
        else:
            low = raw.lower()
            if low == "custom":
                return None
            # Exact, then prefix, then substring — so "tencent", "Tencent COS"
            # and "cos" all land on the same row. Narrowest win first, so an
            # exact name is never treated as ambiguous with a longer one.
            for hits in ([n for n in names if n.lower() == low],
                         [n for n in names if n.lower().startswith(low)],
                         [n for n in names if low in n.lower()]):
                if len(hits) == 1:
                    return hits[0]
                if len(hits) > 1:
                    print("  " + tr("ambiguous", value=raw, hits=hits), file=sys.stderr)
                    break
            else:
                print("  " + tr("notAChoice", value=raw), file=sys.stderr)


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
    print(tr("skipHint") + "\n", file=sys.stderr)

    if _ask_yn(tr("askMeta")):
        meta = {f: _ask(f) for f in META_FIELDS}
        # Skipped fields are omitted; the CLI prints the will-be-cleared note.
        cfg["meta"] = {k: v for k, v in meta.items() if v}

    if _ask_yn(tr("askStorage")):
        choice = _ask_provider()
        if choice is not None:
            provider = PROVIDERS[choice]
            endpoint, region = _ask_endpoint_and_region(provider)
        else:
            endpoint = _ask(tr("endpointUrl"))
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
                f"  {provider.secret_prompt}{tr('secretSuffix')}: "),
            "prefix": _ask(tr("prefix"), DEFAULT_STORAGE_PREFIX),
        }
        cfg["storage"] = {k: v for k, v in storage.items() if v}
        cfg["auto_upload"] = _ask_yn(tr("askAutoUpload"))

    if _ask_yn(tr("askBitrate")):
        cfg["bitrate_kbps"] = _ask_int("bitrate_kbps", "8000", *BITRATE_KBPS)

    if _ask_yn(tr("askResolution")):
        cfg["resolution"] = {
            "width": _ask_int("width", "1920", *RESOLUTION_PX),
            "height": _ask_int("height", "1080", *RESOLUTION_PX),
        }

    if _ask_yn(tr("askWifi")):
        wifi = {"ssid": _ask(tr("ssid"))}
        psk = getpass.getpass(tr("wifiPass"))
        if psk:
            wifi["passphrase"] = psk
        cfg["wifi"] = wifi

    return cfg


def _looks_like_fingerprint(text: str) -> bool:
    """16 lowercase hex — the identifier DeviceState and `VREC` both report."""
    t = text.strip().lower()
    return len(t) == 16 and all(c in "0123456789abcdef" for c in t)


def _key_from_keyring(text: str) -> bytes | None:
    """The keyed-by-fingerprint lookup, or None if this is not a fingerprint.

    Imported lazily: the keyring lives with the recording-crypto reader, and a
    prompt module has no other reason to pull that in.
    """
    if not _looks_like_fingerprint(text):
        return None
    from visio_schema.mcap.crypto import find_key
    return find_key(text.strip().lower())


def interactive_recording_key(args, mint, write_key_file) -> None:
    """Prompt for the recording key, when no key flag was given.

    The flags have always existed, but a guided run is where a fleet owner
    finds out a feature exists at all — and leaving encryption out of the
    prompts made the QR's most security-relevant payload the one thing you had
    to already know to ask for. Someone who types `visio-settings-qr qr --out
    code.png` and answers every question should not end up with a code that
    silently leaves their fleet recording in the clear.

    Populates `args` rather than returning a value, so `_collect_secrets`
    stays the single place an envelope is assembled from.

    `mint` and `write_key_file` are injected to keep this module free of the
    CLI's imports (it is imported BY cli, not the other way round).
    """
    if (args.recording_key_file or args.clear_recording_key
            or args.old_recording_key_file or args.first_provision):
        return                       # explicit flags win; ask nothing

    print("\n" + tr("encHeading"))
    if not _ask_yn(tr("askSetKey")):
        return

    # ONE question, and it asks about a file on the operator's own disk rather
    # than about remote device state. The earlier "is this fleet ALREADY
    # encrypting?" made them guess at something they frequently cannot see —
    # and guessing wrong is only discovered as `bad_old_key` on a rig, after
    # the code is printed and handed out. Whether the rigs are keyed is the
    # device's business; what this flow actually needs to know is whether the
    # operator HOLDS the current key, because that is what goes in the
    # envelope.
    print(tr("encProofHint"), file=sys.stderr)
    while True:
        held = _ask(tr("askCurrentKey"))
        if not held:
            args.first_provision = True
            break
        # A key set from visio-display exists only as a keyring entry — there
        # is no file to point at — and this script runs with no device in
        # front of the operator, so "look it up on the rig" is not advice they
        # can act on. Accept the identifier they actually have.
        found = _key_from_keyring(held)
        if found is not None:
            args.old_recording_key = found
            break
        if _looks_like_fingerprint(held):
            print(tr("notOnKeyring", fp=held), file=sys.stderr)
            continue
        args.old_recording_key_file = held
        break

    if not args.first_provision:
        if _ask_yn(tr("askStopEncrypting")):
            args.clear_recording_key = True
            return

    existing = _ask(tr("askExistingKey"))
    if existing:
        args.recording_key_file = existing
        return

    out = _ask(tr("askKeyOut"), "recording.key")
    key = mint()
    write_key_file(Path(out), key)
    args.recording_key_file = out
