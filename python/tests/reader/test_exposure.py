"""Per-frame exposure timing (`CameraFrameInfo`) attached to `Frame.exposure`.

The contract: every frame of a camera with a `frame_info` stream gets a
`FrameExposure` — exact where an entry exists, interpolated where one is missing,
never `None` mid-stream, because a consumer placing the frame at its exposure
midpoint would otherwise have that one frame snap back to its capture stamp.

The producer computes everything (duration, midpoint offset, gain, rolling-shutter
geometry); the reader converts units, binds the midpoint to each frame's own stamp
and bridges gaps.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest
from _helpers import CAM_K, FRAME_DT, T0, RecBuilder, indexed_frames

from visio_schema.reader import Frame, FrameExposure, ReadoutDirection, Session

CAM0 = "/ego/camera/0"
IMU = "/ego/imu/0/raw"
TOP, BOTTOM = int(ReadoutDirection.TOP_TO_BOTTOM), int(ReadoutDirection.BOTTOM_TO_TOP)


def _rec(tmp_path, n=6, **kw):
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(n))
    b.add_frame_info(CAM0, n=n, **kw)
    return b.write()


def _frames(path) -> list[Frame]:
    return [e for e in Session([path]).stream((CAM0,)) if isinstance(e, Frame)]


def test_absent_frame_info_leaves_exposure_none(tmp_path):
    """No stream: must cost nothing and claim nothing."""
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(4))
    frames = _frames(b.write())
    assert frames and all(f.exposure is None for f in frames)


def test_exposure_joins_every_frame_exactly(tmp_path):
    frames = _frames(_rec(tmp_path, n=6))
    assert len(frames) == 6
    for f in frames:
        e = f.exposure
        assert e is not None and e.interpolated is False
        assert e.exposure_ns == 4_000_000  # wire µs -> reader ns
        assert e.gain == pytest.approx(2.0)
        assert e.mid_ns == f.t_ns - 2_231_000
        assert e.line_delay_ns == 0
        assert e.readout_direction is ReadoutDirection.UNSPECIFIED


def test_midpoint_is_bound_to_each_frames_own_stamp(tmp_path):
    offsets = [-1000, -2000, -3000, -4000]
    frames = _frames(_rec(tmp_path, n=4, mid_offsets_us=offsets))
    for i, f in enumerate(frames):
        assert f.exposure.mid_ns == f.t_ns + offsets[i] * 1000


def test_global_shutter_rows_share_the_midpoint(tmp_path):
    e = _frames(_rec(tmp_path, n=1))[0].exposure
    assert e.row_mid_ns(0, 1080) == e.row_mid_ns(1079, 1080) == e.mid_ns


@pytest.mark.parametrize("direction, first_row_earlier", [(TOP, True), (BOTTOM, False)])
def test_rolling_shutter_row_midpoints_follow_readout_order(
        tmp_path, direction, first_row_earlier):
    """The midpoint names the CENTRE row; row 0 sits (H-1)/2 line delays away, on
    the side the readout direction says."""
    e = _frames(_rec(tmp_path, n=1, line_delay_ns=24_510, direction=direction))[0].exposure
    half_span = 13_223_145  # (1080 - 1) / 2 rows x 24 510 ns, exact
    sign = -1 if first_row_earlier else 1
    assert e.row_mid_ns(539.5, 1080) == e.mid_ns
    top, bottom = e.row_mid_ns(0, 1080), e.row_mid_ns(1079, 1080)
    assert (type(top), type(bottom)) == (int, int), "epoch ns must stay exact"
    assert top == e.mid_ns + sign * half_span
    assert bottom == e.mid_ns - sign * half_span


def test_line_delay_without_direction_is_rejected_loudly(tmp_path):
    """A skew with no direction cannot be placed; refused at read, not at first use."""
    path = _rec(tmp_path, n=2, line_delay_ns=24_510, direction=0)
    with pytest.raises(ValueError, match="no readout direction"):
        _frames(path)


def test_a_hand_built_unoriented_exposure_refuses_row_midpoints():
    e = FrameExposure(exposure_ns=4_000_000, mid_ns=0, gain=1.0, line_delay_ns=24_510,
                      readout_direction=ReadoutDirection.UNSPECIFIED)
    with pytest.raises(ValueError, match="no readout direction"):
        e.row_mid_ns(0, 1080)


def test_a_missing_entry_is_interpolated_not_dropped(tmp_path):
    """The jitter guard. Frame 2 has no entry; it must still carry exposure timing
    between its neighbours', flagged, rather than None."""
    exps = [1000, 2000, 3000, 4000, 5000, 6000]
    offs = [-e // 2 for e in exps]
    gains = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    frames = _frames(_rec(tmp_path, n=6, exposures_us=exps, mid_offsets_us=offs,
                          gains=gains, drop=(2,)))
    assert all(f.exposure is not None for f in frames), "a gap became None"
    gap = frames[2]
    assert gap.exposure.interpolated is True
    assert gap.exposure.exposure_ns == 3_000_000  # midway between 2 ms and 4 ms
    assert gap.exposure.gain == pytest.approx(3.0)
    assert gap.exposure.mid_ns == gap.t_ns - 1_500_000, "offset blended, bound to own stamp"
    for i, f in enumerate(frames):
        if i != 2:
            assert f.exposure.interpolated is False


def test_a_run_of_missing_entries_interpolates_across_the_whole_gap(tmp_path):
    exps = [1000, 2000, 3000, 4000, 5000, 6000]
    frames = _frames(_rec(tmp_path, n=6, exposures_us=exps, drop=(2, 3)))
    got = [f.exposure.exposure_ns for f in frames]
    assert got[2] == round(2_000_000 + 3_000_000 / 3)
    assert got[3] == round(2_000_000 + 3_000_000 * 2 / 3)
    assert [f.exposure.interpolated for f in frames] == [
        False, False, True, True, False, False]


def test_interpolated_nanoseconds_round_to_integers(tmp_path):
    """Thirds of an odd step do not divide: the blend rounds, and stays an int."""
    frames = _frames(_rec(tmp_path, n=4, exposures_us=[1000, 0, 0, 2001],
                          mid_offsets_us=[-1000, 0, 0, -2001], drop=(1, 2)))
    one, two = frames[1].exposure, frames[2].exposure
    assert (one.exposure_ns, two.exposure_ns) == (1_333_667, 1_667_333)
    assert one.mid_ns - frames[1].t_ns == -1_333_667
    assert two.mid_ns - frames[2].t_ns == -1_667_333
    assert all(type(v) is int for v in (one.exposure_ns, one.mid_ns, two.mid_ns))


def test_edges_hold_the_nearest_rather_than_extrapolate(tmp_path):
    """Past the ends of the track there is no bracket. Holding is bounded;
    extrapolating an AE curve is a guess with no bound. The held offset still binds
    to the edge frame's OWN stamp."""
    exps = [1000, 2000, 3000, 4000]
    frames = _frames(_rec(tmp_path, n=4, exposures_us=exps,
                          mid_offsets_us=[-500, -1000, -1500, -2000], drop=(0, 3)))
    first, last = frames[0].exposure, frames[-1].exposure
    assert first.exposure_ns == 2_000_000 and first.interpolated is True
    assert last.exposure_ns == 3_000_000 and last.interpolated is True
    assert first.mid_ns == frames[0].t_ns - 1_000_000
    assert last.mid_ns == frames[-1].t_ns - 1_500_000


def test_readout_geometry_is_not_blended(tmp_path):
    """Line delay and direction are fixed by the sensor mode and mount — a blended
    value would be meaningless — so a gap takes them from the nearer neighbour."""
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(4))
    b.add_frame_info(CAM0, n=1, line_delay_ns=20_000, direction=TOP)
    b.add_frame_info(CAM0, n=1, t0=T0 + 3 * FRAME_DT, line_delay_ns=30_000, direction=BOTTOM)
    frames = _frames(b.write())
    near_lo, near_hi = frames[1].exposure, frames[2].exposure
    assert (near_lo.line_delay_ns, near_lo.readout_direction) == (
        20_000, ReadoutDirection.TOP_TO_BOTTOM)
    assert (near_hi.line_delay_ns, near_hi.readout_direction) == (
        30_000, ReadoutDirection.BOTTOM_TO_TOP)


def test_zero_exposure_is_rejected_loudly(tmp_path):
    """The contract forbids 0: it is a producer bug, never a real exposure."""
    path = _rec(tmp_path, n=3, exposures_us=[4000, 0, 4000])
    with pytest.raises(ValueError, match="exposure_us == 0"):
        _frames(path)


def test_unknown_readout_direction_is_rejected_loudly(tmp_path):
    path = _rec(tmp_path, n=2, line_delay_ns=24_510, direction=7)
    with pytest.raises(ValueError, match="readout_direction 7"):
        _frames(path)


def test_retired_layout_yields_no_exposure_and_says_so(tmp_path, caplog):
    """A recording written with the retired raw layout shares the schema NAME.
    Parsed with the current class it would read as all-zero exposure; the embedded
    descriptor identifies it, so the camera gets no exposure — and a warning."""
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(3))
    b.add_retired_frame_info(CAM0, n=3)
    with caplog.at_level(logging.WARNING, logger="visio_schema.reader.session"):
        frames = _frames(b.write())
    assert len(frames) == 3 and all(f.exposure is None for f in frames)
    assert "retired CameraFrameInfo format" in caplog.text


def test_frame_info_is_not_yielded_as_an_element(tmp_path):
    """It is metadata ON a frame, not a stream element — asking for the camera
    topic must not start yielding a second thing."""
    path = _rec(tmp_path, n=4)
    els = list(Session([path]).stream((CAM0,)))
    assert len(els) == 4
    assert all(isinstance(e, Frame) for e in els)
    assert all(e.topic == CAM0 for e in els)


def test_camera_calibration_still_parses_alongside_frame_info(tmp_path):
    """frame_info rides a `/frame_info` suffix on the camera topic; the calib
    reader keys on `/intrinsics` etc. and must not pick it up."""
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(3))
    b.add_frame_info(CAM0, n=3)
    b.add_camera_calib(CAM0 + "/intrinsics")
    sess = Session([b.write()])
    assert CAM0 in sess.calibration.cams
    assert np.allclose(sess.calibration.cams[CAM0].K, CAM_K)


def test_gap_interpolation_is_bounded_by_the_neighbours(tmp_path):
    """Property: an interpolated value never leaves its bracket, whatever the AE
    did. Guards a sign/weight slip in the blend."""
    exps = [1000, 20000, 2000, 30000, 3000, 40000]
    frames = _frames(_rec(tmp_path, n=6, exposures_us=exps, drop=(1, 3)))
    for i in (1, 3):
        lo = min(exps[i - 1], exps[i + 1]) * 1000
        hi = max(exps[i - 1], exps[i + 1]) * 1000
        assert lo <= frames[i].exposure.exposure_ns <= hi


def test_duplicate_timestamps_are_rejected_loudly(tmp_path):
    """The stamp is unique per stream by contract, so a repeat means two entries
    claim one frame and neither can be trusted — fail rather than guess."""
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(4))
    b.add_frame_info(CAM0, n=4)
    b.add_frame_info(CAM0, n=1, t0=T0 + FRAME_DT)  # a second entry for frame 1
    path = b.write()
    with pytest.raises(ValueError, match="duplicate timestamp"):
        _frames(path)


def test_exposure_unions_across_chunks(tmp_path):
    """A session is many chunks; the track spans all of them. Handed to Session in
    REVERSE order, so nothing may depend on argument order."""
    a, c = tmp_path / "a.mcap", tmp_path / "c.mcap"
    for path, t0, exps in ((a, T0, [1000, 2000]),
                           (c, T0 + 2 * FRAME_DT, [3000, 4000])):
        b = RecBuilder(path)
        b.add_camera(CAM0, indexed_frames(2), t0=t0)
        b.add_frame_info(CAM0, n=2, t0=t0, exposures_us=exps)
        b.write()
    frames = [e for e in Session([c, a]).stream((CAM0,)) if isinstance(e, Frame)]
    assert [f.exposure.exposure_ns for f in frames] == [1_000_000, 2_000_000,
                                                         3_000_000, 4_000_000]
    assert not any(f.exposure.interpolated for f in frames)


def test_interleaved_chunks_are_sorted_before_bisect(tmp_path):
    """The re-sort earns its place only here: RecBuilder sorts within a file, so
    a single chunk can never produce an unordered track. Two chunks whose frames
    INTERLEAVE can — and an unsorted list silently mis-assigns every bisect."""
    odd, even = tmp_path / "odd.mcap", tmp_path / "even.mcap"
    for path, idxs in ((even, (0, 2, 4)), (odd, (1, 3, 5))):
        b = RecBuilder(path)
        for i in idxs:
            b.add_camera(CAM0, indexed_frames(1), t0=T0 + i * FRAME_DT)
            b.add_frame_info(CAM0, n=1, t0=T0 + i * FRAME_DT,
                             exposures_us=[1000 * (i + 1)])
        b.write()
    frames = [e for e in Session([odd, even]).stream((CAM0,)) if isinstance(e, Frame)]
    assert len(frames) == 6
    # Asserted per-frame by its OWN stamp, not by emission order: interleaved
    # chunks are wider than the reorder window (a shape `Session` does not claim
    # to support — real chunks are time-sequential), so the frames come out
    # unordered here. The join is on timestamp, which is exactly the point.
    for f in frames:
        i = (f.t_ns - T0) // FRAME_DT
        assert f.exposure.exposure_ns == 1_000_000 * (i + 1)
        assert f.exposure.interpolated is False


def test_imu_only_stream_does_not_index_exposure(tmp_path):
    """The index is a full second pass over the recording's bytes. A caller that
    streams no camera must not pay it."""
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(4))
    b.add_frame_info(CAM0, n=4)
    b.add_imu_bundle(IMU, T0, [0, 5_000_000])
    sess = Session([b.write()])
    assert list(sess.stream((IMU,)))
    assert sess._exposure is None, "exposure was indexed for an IMU-only stream"


def test_frames_and_exposures_line_up_after_a_dropped_video_frame(tmp_path):
    """The join is on TIMESTAMP, never on index. Drop a video frame and the
    surviving frames must keep their own exposures, not shift by one."""
    exps = [1000, 2000, 3000, 4000, 5000, 6000]
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(6), drop=(2,))
    b.add_frame_info(CAM0, n=6, exposures_us=exps)
    frames = _frames(b.write())
    for f in frames:
        i = (f.t_ns - T0) // FRAME_DT
        assert f.exposure.exposure_ns == exps[i] * 1000, f"exposure shifted at {i}"
        assert f.exposure.interpolated is False
