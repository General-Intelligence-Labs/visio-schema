"""Fixtures wrapping the MCAP builders in ``_helpers``."""

from __future__ import annotations

# CI runs one leg with NO extras, to prove the wire contract installs and
# passes on its own. This suite needs the [reader] extra (scipy at import
# time in _helpers, av to decode),
# so without it the directory is skipped rather than failing at collection
# — an ImportError here would read as a broken package rather than an
# absent optional dependency.
import importlib.util as _iu

import pytest

_MISSING = [p for p in ("scipy", "av") if _iu.find_spec(p) is None]
if _MISSING:
    collect_ignore_glob = ["*.py"]
else:
    # Guarded, because collect_ignore_glob only spares the TEST files — this
    # conftest still executes. An unconditional `from _helpers import ...` here
    # imports scipy and raises during conftest collection, before the ignore can
    # apply, which is exactly the failure the guard above was written to
    # prevent. The fixtures below name these at CALL time, and they are never
    # called when the suite is ignored.
    from _helpers import RecBuilder, indexed_frames, stereo_calib_builder


@pytest.fixture
def rec(tmp_path):
    """Factory: (name, device=None, capture=None) -> a RecBuilder."""

    def _make(name="rec.mcap", device=None, capture=None):
        return RecBuilder(tmp_path / name, device=device, capture=capture)

    return _make


@pytest.fixture
def stereo_calib_rec(tmp_path):
    """Factory: (n_frames=0) -> a written recording with stereo + calibration."""

    def _make(n_frames=0, name="rec.mcap"):
        b = stereo_calib_builder(tmp_path / name)
        if n_frames:
            b.add_camera("/ego/camera/0", indexed_frames(n_frames))
            b.add_camera("/ego/camera/1", indexed_frames(n_frames))
        return b.write(), b

    return _make
