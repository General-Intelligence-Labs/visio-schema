"""Fixtures for the dataset-layer tests. scipy-free on purpose: these run
inside the built wheel, whose test env carries only the base deps + pyarrow."""

import numpy as np
import pytest


@pytest.fixture
def solid_frames():
    """Factory: n RGB frames whose red channel ramps per frame (verifiable)."""

    def _make(n, h=48, w=64):
        out = []
        for i in range(n):
            f = np.zeros((h, w, 3), dtype=np.uint8)
            f[:, :, 0] = (40 + i * 20) % 256
            out.append(f)
        return out

    return _make
