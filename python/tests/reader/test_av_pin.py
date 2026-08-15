"""The PyAV bound has exactly one spelling.

`_decode.AV_DECODE_CHANGED_AT` is where the decoder's output was measured to
move; `pyproject.toml` is what actually gets installed. Two independently
maintained copies of a version is the drift this codebase keeps paying for
elsewhere — so assert the round trip rather than trusting they match.

Why the bound exists: running the same reader over the same recording gives
identical pixels for every PyAV below 17.0.0 (12.3.0, 13.1.0, 14.x, 15.x, 16.x
were all measured to the same bytes over 300 real ego access units) and different
pixels at and above it. Anything downstream that compares two runs is void if
they straddle that line.

The dependency's FLOOR is a different constraint with a different owner — the
viewer's `av.codec.hwaccel` probe, absent before 14.2 — so it is asserted
separately here rather than folded into one number.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from visio_schema.reader._decode import AV_DECODE_CHANGED_AT, _version_tuple

if sys.version_info >= (3, 11):
    import tomllib
else:
    tomllib = pytest.importorskip("tomli")

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _declared_av_range() -> tuple[str, str]:
    """The ``>=lo,<hi`` bounds declared for `av`, as written."""
    data = tomllib.loads(_PYPROJECT.read_text())
    for dep in data["project"]["dependencies"]:
        m = re.fullmatch(r"av>=([0-9][0-9.]*),<([0-9][0-9.]*)", dep.strip())
        if m:
            return m.group(1), m.group(2)
    raise AssertionError(
        "no `av>=lo,<hi` range in [project.dependencies]. The upper bound is "
        "what makes two runs' decoded output comparable; an open ceiling "
        "silently lets a release through that changes every pixel."
    )


def test_the_declared_ceiling_is_where_decode_was_measured_to_change():
    _lo, hi = _declared_av_range()
    assert _version_tuple(hi) == AV_DECODE_CHANGED_AT[: len(_version_tuple(hi))], (
        "`_decode.AV_DECODE_CHANGED_AT` and the `av<` ceiling in pyproject.toml "
        "disagree. Update both, and re-measure decode output before moving "
        "either."
    )


def test_the_declared_floor_admits_the_viewers_hwaccel_api():
    """`av.codec.hwaccel` does not exist before 14.2, and `display._make_decoder`
    reaches for it unguarded. A lower floor ships a viewer that raises
    `AttributeError` on its first transcode."""
    lo, _hi = _declared_av_range()
    assert _version_tuple(lo) >= (14, 2)


def test_the_runtime_guard_fires_above_the_bound(caplog):
    """The warning must actually reach a log — a guard nobody sees is not one."""
    import logging
    import types

    from visio_schema.reader import _decode

    _decode._av_checked = False
    try:
        with caplog.at_level(logging.WARNING, logger="visio_schema.reader"):
            _decode._check_av_version(types.SimpleNamespace(__version__="17.0.0"))
        assert "17.0.0" in caplog.text
    finally:
        _decode._av_checked = False


def test_the_runtime_guard_is_quiet_across_the_whole_compatible_set(caplog):
    """Every version below the bound decodes the same, so none of them is worth
    a word — warning on "not the exact one I was built against" was noise that
    trained people to ignore the one case that matters."""
    import logging
    import types

    from visio_schema.reader import _decode

    for version in ("12.3.0", "13.1.0", "14.2.0", "15.1.0", "16.1.0"):
        caplog.clear()
        _decode._av_checked = False
        with caplog.at_level(logging.WARNING, logger="visio_schema.reader"):
            _decode._check_av_version(types.SimpleNamespace(__version__=version))
        assert caplog.text == "", f"warned about {version}, which decodes the same"
    _decode._av_checked = False


def test_an_unreadable_version_is_reported_rather_than_assumed(caplog):
    import logging
    import types

    from visio_schema.reader import _decode

    _decode._av_checked = False
    try:
        with caplog.at_level(logging.WARNING, logger="visio_schema.reader"):
            _decode._check_av_version(types.SimpleNamespace(__version__=""))
        assert "Cannot determine" in caplog.text
    finally:
        _decode._av_checked = False
