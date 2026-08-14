"""The PyAV pin has exactly one spelling.

`_decode.PINNED_AV` is what the decoder's output was measured against and what it
warns about at runtime; `pyproject.toml` is what actually gets installed. Two
independently-maintained copies of a version is the drift this codebase keeps
paying for elsewhere — so assert the round trip rather than trusting they match.

Why the pin exists at all: running the same reader over the same recording under
PyAV 12.3.0 and 17.0.0 gives identical element counts, identical topics and an
identical undecoded pass, but DIFFERENT pixels in every decode mode. Anything
downstream that compares two runs is void if they used different PyAVs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from visio_schema.reader._decode import PINNED_AV

if sys.version_info >= (3, 11):
    import tomllib
else:
    tomllib = pytest.importorskip("tomli")

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _declared_av_pin() -> str:
    data = tomllib.loads(_PYPROJECT.read_text())
    for dep in data["project"]["dependencies"]:
        m = re.fullmatch(r"av==([0-9][0-9.]*)", dep.strip())
        if m:
            return m.group(1)
    raise AssertionError(
        "no exact `av==` pin in [project.dependencies]. The reader's decode "
        "output is only reproducible against one PyAV; a floor or a range there "
        "silently makes two runs incomparable."
    )


def test_pinned_av_matches_the_declared_dependency():
    assert PINNED_AV == _declared_av_pin(), (
        "visio_schema.reader._decode.PINNED_AV and the `av==` pin in "
        "pyproject.toml disagree. Update both, and re-measure before changing "
        "the version at all."
    )


def test_the_runtime_guard_fires_on_a_mismatch(caplog):
    """The warning must actually reach a log — a guard nobody sees is not one."""
    import logging
    import types

    from visio_schema.reader import _decode

    _decode._av_checked = False
    try:
        with caplog.at_level(logging.WARNING, logger="visio_schema.reader"):
            _decode._check_av_version(types.SimpleNamespace(__version__="0.0.0-fake"))
        assert "0.0.0-fake" in caplog.text
        assert PINNED_AV in caplog.text
    finally:
        _decode._av_checked = False


def test_the_runtime_guard_is_quiet_on_a_match(caplog):
    import logging
    import types

    from visio_schema.reader import _decode

    _decode._av_checked = False
    try:
        with caplog.at_level(logging.WARNING, logger="visio_schema.reader"):
            _decode._check_av_version(types.SimpleNamespace(__version__=PINNED_AV))
        assert caplog.text == ""
    finally:
        _decode._av_checked = False
