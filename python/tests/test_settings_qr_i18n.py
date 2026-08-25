"""Translations for the settings-QR tool.

The people running this are a client's admins provisioning rigs in the room
where the rigs are. What they read here decides whether a fleet ends up
encrypted, so a missing or broken string is a provisioning failure, not a
cosmetic one.
"""
from __future__ import annotations

import builtins

import pytest

from visio_schema.settings_qr import i18n
from visio_schema.settings_qr.i18n import LANGUAGES, detect_language, set_language, tr


@pytest.fixture(autouse=True)
def _english_by_default(monkeypatch):
    for var in ("VISIO_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    set_language("en")
    yield
    set_language("en")


# -- the table ----------------------------------------------------------- #

def test_every_string_exists_in_every_language():
    missing = {lang: sorted(k for k, v in i18n._STRINGS.items() if lang not in v)
               for lang in LANGUAGES}
    assert not any(missing.values()), missing


def test_no_translation_is_left_empty():
    blank = [(k, lang) for k, v in i18n._STRINGS.items()
             for lang in LANGUAGES if not v[lang].strip()]
    assert not blank


def test_a_translation_keeps_its_format_placeholders():
    """A dropped `{fp}` renders a warning that names no key.

    These strings carry the fingerprint, the path and the QR version — the
    only actionable parts of the messages they appear in.
    """
    import re
    for key, langs in i18n._STRINGS.items():
        want = set(re.findall(r"\{(\w+)", langs["en"]))
        for lang in LANGUAGES:
            got = set(re.findall(r"\{(\w+)", langs[lang]))
            assert got == want, f"{key}/{lang}: {got} != {want}"


def test_the_untranslated_key_falls_back_to_english_not_a_crash():
    # A half-provisioned fleet is worse than an ugly prompt, so a gap must
    # never raise in the middle of a key change.
    set_language("zh")
    i18n._STRINGS["_probe"] = {"en": "only english"}
    try:
        assert tr("_probe") == "only english"
    finally:
        del i18n._STRINGS["_probe"]


def test_an_unknown_key_renders_as_itself():
    assert tr("noSuchStringAnywhere") == "noSuchStringAnywhere"


# -- language detection --------------------------------------------------- #

@pytest.mark.parametrize("value, expect", [
    ("zh_CN.UTF-8", "zh"), ("zh", "zh"), ("zh-Hans", "zh"),
    ("en_US.UTF-8", "en"), ("C", "en"), ("fr_FR.UTF-8", "en"),
])
def test_the_locale_picks_the_language(monkeypatch, value, expect):
    monkeypatch.setenv("LANG", value)
    assert detect_language() == expect


def test_visio_lang_outranks_the_locale(monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("VISIO_LANG", "zh")
    assert detect_language() == "zh"


def test_a_set_but_unknown_locale_does_not_fall_through(monkeypatch):
    # LC_ALL is the override; if it says French we answer English rather than
    # letting a lower-priority LANG=zh win a decision LC_ALL already made.
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert detect_language() == "en"


def test_an_unknown_explicit_language_falls_back_to_detection(monkeypatch):
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert set_language("kr") == "zh"


# -- the language prompt --------------------------------------------------- #

def _choose(answer, monkeypatch):
    from visio_schema.settings_qr.interactive import ask_language
    answers = iter([answer])
    monkeypatch.setattr(builtins, "input", lambda *a, **k: next(answers))
    return ask_language()


@pytest.mark.parametrize("typed, expect", [
    ("1", "en"), ("2", "zh"), ("en", "en"), ("zh", "zh"),
    ("English", "en"), ("中文", "zh"), ("ZH", "zh"),
])
def test_the_prompt_accepts_a_number_a_code_or_the_language_name(
        monkeypatch, typed, expect):
    assert _choose(typed, monkeypatch) == expect


def test_pressing_enter_takes_the_detected_language(monkeypatch):
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert _choose("", monkeypatch) == "zh"


def test_the_prompt_is_shown_in_both_languages(monkeypatch, capsys):
    # It is the one question asked before we know what the operator reads.
    _choose("1", monkeypatch)
    shown = capsys.readouterr().err
    assert "English" in shown and "中文" in shown
