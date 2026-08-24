"""Every string the launcher page asks for exists in both languages.

A missing key is not an error anywhere — `tr()` falls back to English and then
to the key itself, so the page silently renders `cfgEncRotate` at an operator.
That is exactly the kind of thing nobody notices until a customer screenshot
arrives, so it is pinned here instead.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "visio_schema" / "display" / "static"
_INDEX = _STATIC / "index.html"
_APP_JS = _STATIC / "app.js"

# `key: "value"`, consuming the whole string literal so a colon INSIDE a value
# (…opens it:") is not mistaken for the next key.
_ENTRY = re.compile(r'(\w+)\s*:\s*"((?:[^"\\]|\\.)*)"')
_LANG_BLOCK = re.compile(r"^      (\w+): \{$(.*?)^      \},$", re.M | re.S)


def _locales() -> dict[str, dict[str, str]]:
    text = _INDEX.read_text(encoding="utf-8")
    return {lang: dict(_ENTRY.findall(body))
            for lang, body in _LANG_BLOCK.findall(text)}


def test_the_page_ships_both_languages():
    assert set(_locales()) == {"en", "zh"}


def test_no_language_is_missing_a_string():
    locales = _locales()
    en, zh = set(locales["en"]), set(locales["zh"])
    assert en - zh == set(), f"untranslated into zh: {sorted(en - zh)}"
    assert zh - en == set(), f"present only in zh: {sorted(zh - en)}"


def test_no_string_is_left_empty():
    for lang, entries in _locales().items():
        blank = sorted(k for k, v in entries.items() if not v.strip())
        assert not blank, f"{lang} has empty strings: {blank}"


def _markup_keys() -> set[str]:
    text = _INDEX.read_text(encoding="utf-8")
    return set(re.findall(r'data-i18n(?:-ph)?="([^"]+)"', text))


def _script_keys() -> set[str]:
    # Literal tr("…") only: a few call sites build the key from a lookup table
    # and cannot be resolved statically.
    return set(re.findall(r'\btr\("([^"]+)"\)', _APP_JS.read_text(encoding="utf-8")))


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_every_key_the_page_asks_for_is_translated(lang):
    have = set(_locales()[lang])
    want = _markup_keys() | _script_keys()
    assert want - have == set(), f"{lang} is missing: {sorted(want - have)}"


def test_the_encryption_panel_is_fully_translated():
    # The panel this suite was added for. Named explicitly so a future edit
    # that drops a string fails with the reason rather than a bare diff.
    want = {"cfgEnc", "cfgEncNext", "cfgEncSet", "cfgEncRotate", "cfgEncClear",
            "cfgEncCurrent", "cfgEncWarn", "cfgEncOn", "cfgEncOff",
            "cfgEncRequired", "cfgEncUnsupported", "cfgEncConfirmClear",
            "cfgEncKeyed", "cfgEncCleared", "cfgEncRig", "cfgEncShow",
            "cfgEncHide", "cfgEncKeyWarn", "mcapNeedsKey"}
    for lang, entries in _locales().items():
        assert want <= set(entries), (
            f"{lang} is missing: {sorted(want - set(entries))}")


def test_the_page_reads_a_key_only_from_the_reveal_endpoint():
    """The admin may see their own key; a DEVICE may never disclose one.

    The rig has no command that reads its key back — the operator holding it is
    the adversary — so the page must source key material from this computer's
    keyring (via /api/config/recording-key/reveal) and from nowhere else. A
    future edit that renders a key off DeviceState is the thing to catch.
    """
    js = _APP_JS.read_text(encoding="utf-8")
    assert "recording-key/reveal" in js, "reveal is how the key reaches the page"
    # A word-boundary regex, not a substring: `"...\b"` in a Python string is
    # U+0008, so the old check could never match anything — and a plain
    # substring cannot work either, because `st.recording_key_fingerprint` is
    # legitimate. Only a bare `.recording_key` means the wire started echoing
    # the key itself.
    assert not re.search(r"\.recording_key(?!\w)", js), (
        "the page must never read a raw key off DeviceState")
    assert "recording_key_fingerprint" in js, "the badge reads the fingerprint"


def test_the_key_box_masks_at_full_width():
    """Masked and revealed must be the same length, so toggling cannot reflow.

    A shorter mask would move the buttons under the operator's cursor mid-click
    and would misrepresent how long the key they must back up really is.
    """
    js = _APP_JS.read_text(encoding="utf-8")
    assert "KEY_HEX_CHARS = 64" in js, "a key is 32 bytes = 64 hex characters"
    assert "KEY_MASK_CHAR.repeat(KEY_HEX_CHARS)" in js, (
        "hiding must mask to the same width, not blank the box")
    # ASCII, or the mask falls back off the monospace face and stops being
    # exactly as wide as the hex it stands in for.
    assert 'KEY_MASK_CHAR = "*"' in js
    # The box must hold its size on an UNKEYED rig too, which means the
    # placeholder still lays out — hidden, not removed. Emptying it or using
    # display:none would collapse the row.
    assert 'style.visibility = _encFingerprint ? "visible" : "hidden"' in js
    assert 'textContent = ""' not in js.split("function hideKey")[1][:400]
    # And the box must not collapse when there is genuinely no key to mask.
    assert 'display:inline-block' in _INDEX.read_text(encoding="utf-8")


def test_a_dead_link_hides_the_command_panel():
    """Regression: "ended" left every config button visible but doomed.

    A closed link means the reader thread is gone, so every command answers
    "no device connected" — while the panel kept showing the last DeviceState
    and therefore looked live. "error" was handled and "ended" was not, even
    though the adjacent poll-stopping line already treated them as one state.
    """
    js = _APP_JS.read_text(encoding="utf-8")
    # The whole assignment, however it happens to be wrapped.
    m = re.search(r'\$\("config"\)\.hidden\s*=(.*?);', js, re.S)
    assert m, 'the config panel visibility rule moved'
    condition = m.group(1)
    assert 'state === "ended"' in condition, (
        'the config panel must hide on a terminal link, not just on "error"')
    assert 'state === "error"' in condition
