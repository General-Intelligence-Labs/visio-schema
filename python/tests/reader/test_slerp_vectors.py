"""The orientation kernel against the cross-language golden file.

`tests/golden/slerp_vectors.txt` is what the C++ `SlerpXyzw` replays too. This
side asserts the Python reference still produces those exact bytes, so the file
cannot silently go stale behind a kernel change: regenerate it with
scripts/gen_slerp_vectors.py, and the C++ suite then tells you whether the twin
followed.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
import pytest

from visio_schema.reader import QUATERNION_SCHEMA, Orientation, blend
from visio_schema.reader.interp import slerp_xyzw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _golden import load

CASES = load("slerp_vectors.txt")


def test_the_golden_file_covers_every_kernel_branch():
    for name in ("arc_mid", "short_arc_flip", "near_parallel", "endpoint_lo",
                 "endpoint_hi", "extrapolate"):
        assert name in CASES


@pytest.mark.parametrize("name", sorted(CASES))
def test_kernel_reproduces_the_golden_output(name):
    v = struct.unpack("<13d", CASES[name])
    q0, q1, w, want = np.array(v[0:4]), np.array(v[4:8]), v[8], np.array(v[9:13])
    got = slerp_xyzw(q0, q1, w)
    # Bit-for-bit on the reference implementation: this file WAS produced by it.
    assert got.tobytes() == want.tobytes()


def test_orientation_blends_through_the_kernel():
    lo = Orientation("/glove_right/imu/3/quat", 1_000, np.array([0.0, 0.0, 0.0, 1.0]))
    hi = Orientation("/glove_right/imu/3/quat", 3_000,
                     np.array([0.0, 0.0, np.sin(np.pi / 4), np.cos(np.pi / 4)]))
    mid = blend(lo, hi, 2_000)
    assert mid.t_ns == 2_000
    assert mid.topic == lo.topic
    np.testing.assert_array_equal(mid.q, slerp_xyzw(lo.q, hi.q, 0.5))


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize("end", ["lo", "hi"])
def test_orientation_refuses_non_finite_quaternions(bad, end):
    valid = np.array([0.0, 0.0, 0.0, 1.0])
    invalid = np.array([bad, 0.0, 0.0, 1.0])
    lo = Orientation("/imu/quat", 1_000, invalid if end == "lo" else valid)
    hi = Orientation("/imu/quat", 3_000, invalid if end == "hi" else valid)
    with pytest.raises(ValueError, match="not 1"):
        blend(lo, hi, 2_000)


def test_the_imu_quaternion_schema_has_an_adapter():
    # Without one, `/imu/<n>/quat` falls back to an opaque Record, which `sync`
    # can only ever pick nearest — never interpolate.
    from visio_schema.reader import registered_schemas

    assert QUATERNION_SCHEMA in registered_schemas()
