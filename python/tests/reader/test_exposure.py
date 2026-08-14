"""Per-frame exposure (`CameraFrameInfo`) attached to `Frame.exposure`.

The stream is opt-in on the device and off by default, so the ABSENT case is the
common one and must stay free. When present, the contract is: every frame gets a
`FrameExposure`, exact where an entry exists and interpolated where one is missing
— never `None` mid-stream, because a consumer applying an exposure-derived timing
correction would otherwise have that one frame snap back to uncorrected.

Gaps cannot be produced on demand from hardware (a real capture has a complete
counter and, in a static scene, a frozen AE), so the interpolation cases are pinned
here on synthetic recordings and the join itself is verified against a real one.
"""

from __future__ import annotations

import numpy as np
import pytest
from _helpers import CAM_K, FRAME_DT, T0, RecBuilder, indexed_frames

from visio_schema.reader import Frame, Session

CAM0 = "/ego/camera/0"
IMU = "/ego/imu/0/raw"


def _rec(tmp_path, n=6, **kw):
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(n))
    b.add_frame_info(CAM0, n=n, **kw)
    return b.write()


def _frames(path) -> list[Frame]:
    return [e for e in Session([path]).stream((CAM0,)) if isinstance(e, Frame)]


def test_absent_frame_info_leaves_exposure_none(tmp_path):
    """The default device config. Must cost nothing and claim nothing."""
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(4))
    frames = _frames(b.write())
    assert frames and all(f.exposure is None for f in frames)


def test_exposure_joins_every_frame_exactly(tmp_path):
    frames = _frames(_rec(tmp_path, n=6))
    assert len(frames) == 6
    for i, f in enumerate(frames):
        assert f.exposure is not None
        assert f.exposure.interpolated is False
        assert f.exposure.isp_frame_id == i  # the diagnostic, carried through
        assert f.exposure.exposure_time_s == pytest.approx(0.004002193)


def test_line_time_is_derived_from_sensor_timing(tmp_path):
    """HTS / pclk -> the rolling-shutter row delay, which replaced the
    never-populated `Calibration.rs_line_delay_ns`."""
    f = _frames(_rec(tmp_path, n=2))[0]
    # 612 / 44.65 MHz = 13.706 us, matching the AR0234's v4l2 subdev measurement
    assert f.exposure.line_time_ns == pytest.approx(13706.6, abs=0.1)
    assert not hasattr(Session([_rec(tmp_path, n=2)]).calibration, "rs_line_delay_ns")


def test_a_missing_entry_is_interpolated_not_dropped(tmp_path):
    """The jitter guard. Frame 2 has no entry; it must still carry an exposure
    between its neighbours', flagged, rather than None."""
    exps = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006]
    frames = _frames(_rec(tmp_path, n=6, exposures=exps, drop=(2,)))
    assert all(f.exposure is not None for f in frames), "a gap became None"
    gap = frames[2]
    assert gap.exposure.interpolated is True
    assert gap.exposure.isp_frame_id is None, "an interpolated value has no source id"
    # midway between its bracketing entries (2 ms at index 1, 4 ms at index 3)
    assert gap.exposure.exposure_time_s == pytest.approx(0.003)
    for i, f in enumerate(frames):
        if i != 2:
            assert f.exposure.interpolated is False


def test_a_run_of_missing_entries_interpolates_across_the_whole_gap(tmp_path):
    exps = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006]
    frames = _frames(_rec(tmp_path, n=6, exposures=exps, drop=(2, 3)))
    got = [f.exposure.exposure_time_s for f in frames]
    # 1 ms .. 5 ms bracket spanning indices 1..4, sampled on a uniform grid
    assert got[2] == pytest.approx(0.002 + (0.005 - 0.002) * (1 / 3))
    assert got[3] == pytest.approx(0.002 + (0.005 - 0.002) * (2 / 3))
    assert [f.exposure.interpolated for f in frames] == [
        False, False, True, True, False, False]


def test_edges_hold_the_nearest_rather_than_extrapolate(tmp_path):
    """Past the ends of the track there is no bracket. Holding is bounded;
    extrapolating an AE curve is a guess with no bound."""
    exps = [0.001, 0.002, 0.003, 0.004]
    frames = _frames(_rec(tmp_path, n=4, exposures=exps, drop=(0, 3)))
    assert frames[0].exposure.exposure_time_s == pytest.approx(0.002)  # from idx 1
    assert frames[-1].exposure.exposure_time_s == pytest.approx(0.003)  # from idx 2
    assert frames[0].exposure.interpolated is True
    assert frames[-1].exposure.interpolated is True


def test_static_sensor_timing_is_not_blended(tmp_path):
    """Interpolating a pixel clock would be meaningless, not merely approximate —
    the AE outputs vary continuously, the sensor mode does not."""
    frames = _frames(_rec(tmp_path, n=5, drop=(2,)))
    gap = frames[2]
    assert gap.exposure.interpolated is True
    assert gap.exposure.line_time_ns == frames[0].exposure.line_time_ns
    assert gap.exposure.frame_length_lines == frames[0].exposure.frame_length_lines
    # integer register fields come from a real operating point, not a fraction
    assert isinstance(gap.exposure.coarse_integration_time_lines, int)


def test_absent_sensor_timing_is_none_not_zero(tmp_path):
    """Reachable: `AiqManager::prime_stats` gives up for a whole session when
    `queryExpResInfo` fails, and HTS/pclk then read 0 on the wire.

    `None`, never 0.0 — a 0 ns row delay is what a *global-shutter* sensor
    legitimately reports, so returning it would silently turn a consumer's
    rolling-shutter model into a no-op instead of telling it the timing is
    unknown. The rest of the exposure is still good and still attached."""
    f = _frames(_rec(tmp_path, n=2, pclk=0.0))[0]
    assert f.exposure is not None
    assert f.exposure.line_time_ns is None
    assert f.exposure.exposure_time_s == pytest.approx(0.004002193)


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
    exps = [0.001, 0.020, 0.002, 0.030, 0.003, 0.040]
    frames = _frames(_rec(tmp_path, n=6, exposures=exps, drop=(1, 3)))
    for i in (1, 3):
        lo = min(exps[i - 1], exps[i + 1])
        hi = max(exps[i - 1], exps[i + 1])
        assert lo <= frames[i].exposure.exposure_time_s <= hi


def test_duplicate_timestamps_are_rejected_loudly(tmp_path):
    """Schema 0.7.0 makes the stamp unique BY CONSTRUCTION, so a repeat means two
    entries claim one frame and neither can be trusted. Pre-0.7.0 recordings carry
    this on 4-7% of frames; silently taking the first is the exact failure the
    producer change removed, so reading one must fail rather than guess."""
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
    for path, t0, exps in ((a, T0, [0.001, 0.002]),
                           (c, T0 + 2 * FRAME_DT, [0.003, 0.004])):
        b = RecBuilder(path)
        b.add_camera(CAM0, indexed_frames(2), t0=t0)
        b.add_frame_info(CAM0, n=2, t0=t0, exposures=exps)
        b.write()
    frames = [e for e in Session([c, a]).stream((CAM0,)) if isinstance(e, Frame)]
    got = [f.exposure.exposure_time_s for f in frames]
    assert got == pytest.approx([0.001, 0.002, 0.003, 0.004])
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
                             exposures=[0.001 * (i + 1)])
        b.write()
    frames = [e for e in Session([odd, even]).stream((CAM0,)) if isinstance(e, Frame)]
    assert len(frames) == 6
    # Asserted per-frame by its OWN stamp, not by emission order: interleaved
    # chunks are wider than the reorder window (a shape `Session` does not claim
    # to support — real chunks are time-sequential), so the frames come out
    # unordered here. The join is on timestamp, which is exactly the point.
    for f in frames:
        i = (f.t_ns - T0) // FRAME_DT
        assert f.exposure.exposure_time_s == pytest.approx(0.001 * (i + 1))
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
    exps = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006]
    b = RecBuilder(tmp_path / "r.mcap")
    b.add_camera(CAM0, indexed_frames(6), drop=(2,))
    b.add_frame_info(CAM0, n=6, exposures=exps)
    frames = _frames(b.write())
    for f in frames:
        i = (f.t_ns - T0) // FRAME_DT
        assert f.exposure.exposure_time_s == pytest.approx(exps[i]), \
            f"exposure shifted at {i}"
        assert f.exposure.interpolated is False
