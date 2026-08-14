"""Fixtures wrapping the MCAP builders in ``_helpers``."""

from __future__ import annotations

import pytest
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
