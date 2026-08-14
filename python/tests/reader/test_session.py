"""Session: calibration, cheap topics, device prefix, concat, metadata, IMU."""

from __future__ import annotations

import numpy as np
import pytest
from _helpers import (
    CAM_K,
    FRAME_DT,
    T0,
    RecBuilder,
    indexed_frames,
)

from visio_schema.reader import Frame, ImuSample, Session


def test_calibration_parse(stereo_calib_rec):
    path, _ = stereo_calib_rec()
    cal = Session([path]).calibration
    assert set(cal.cams) == {"/ego/camera/0", "/ego/camera/1"}
    assert cal.cams["/ego/camera/1"].width == 640
    assert np.allclose(cal.cams["/ego/camera/0"].K, CAM_K)
    assert cal.baseline_m == pytest.approx(0.062, abs=1e-6)
    assert cal.cam_imu_dt_ns == 39_000_000
    assert cal.imu_rate_hz == 200.0


def test_calibration_cam_imu_extrinsic(stereo_calib_rec):
    """T_cam_imu is kalibr's matrix verbatim — used with NO inversion."""
    path, _ = stereo_calib_rec()
    cal = Session([path]).calibration
    assert cal.T_cam_imu is not None
    assert cal.T_cam_imu.shape == (4, 4)
    from _helpers import IMU_QUAT
    from scipy.spatial.transform import Rotation
    # asymmetric on purpose: R != R.T, so a transposed block fails here
    expect = Rotation.from_quat(IMU_QUAT).as_matrix()
    assert not np.allclose(expect, expect.T), "fixture must be asymmetric to be useful"
    assert np.allclose(cal.T_cam_imu[:3, :3], expect)
    assert np.allclose(cal.T_cam_imu[:3, 3], [-0.018, 0.026, -0.002])
    assert np.allclose(cal.T_cam_imu[3], [0, 0, 0, 1])
    # the IMU tf must NOT be mistaken for the stereo one
    assert np.allclose(cal.stereo_T, [-0.062, 0, 0])


def test_stereo_extrinsic_has_no_arbitrary_fallback(rec):
    """A recording with only /tf must not yield a runtime pose as the stereo tf.

    Regression: `_pick_stereo` used to fall back to `next(iter(...))`. Older
    recordings carry no `camera/1/extrinsics`, only `/tf` (`world -> ego/imu/0`),
    so that fallback silently returned a *runtime head pose* as a stereo
    extrinsic — rectifying with it produces garbage instead of an error.
    """
    b = rec()
    b.add_camera_calib("/ego/camera/0/intrinsics")
    b.add_camera_calib("/ego/camera/1/intrinsics")
    b.add_extrinsics("/tf", T=(1.5, 2.5, 3.5), child="ego/imu/0")
    b.write()
    cal = Session([b.path]).calibration
    assert cal.stereo_R is None and cal.stereo_T is None and cal.baseline_m is None
    assert cal.T_cam_imu is None  # /tf is not an extrinsics topic either


def test_calib_topics_excludes_runtime_tf(stereo_calib_rec):
    """`/tf` shares FrameTransform's schema but is a pose stream, not calibration."""
    path, b = stereo_calib_rec()
    b.add_extrinsics("/tf", T=(9.0, 9.0, 9.0), child="ego/imu/0")
    path = b.write()
    sess = Session([path])
    assert "/tf" not in sess._calib_topics()
    # and the real extrinsics still win
    assert np.allclose(sess.calibration.stereo_T, [-0.062, 0, 0])


def test_topics_cheap_summary(stereo_calib_rec):
    path, _ = stereo_calib_rec(n_frames=4)
    infos = {t.topic: t for t in Session([path]).topics()}
    assert "/ego/camera/0" in infos
    assert "/ego/camera/0/intrinsics" in infos
    assert infos["/ego/camera/0"].message_count == 4
    assert infos["/ego/camera/0"].schema_name == "foxglove.CompressedVideo"


def test_device_prefix_stripped(rec):
    b = rec(device="GILABS-AABBCCDD")
    b.add_camera("/ego/camera/0", indexed_frames(3))
    b.write()
    s = Session([b.path])
    assert s.device == "GILABS-AABBCCDD"
    assert {t.topic for t in s.topics()} == {"/ego/camera/0"}
    frames = list(s.stream())
    assert len(frames) == 3
    assert all(f.topic == "/ego/camera/0" for f in frames)


def test_multi_file_ordered_by_time(tmp_path):
    early = RecBuilder(tmp_path / "z_early.mcap")
    early.add_camera("/ego/camera/0", indexed_frames(3), t0=T0).write()
    late = RecBuilder(tmp_path / "a_late.mcap")
    late.add_camera("/ego/camera/0", indexed_frames(3), t0=T0 + 100 * FRAME_DT).write()
    # filename order (a_late < z_early) must NOT determine stream order
    s = Session([late.path, early.path])
    ts = [f.t_ns for f in s.stream()]
    assert ts == sorted(ts)
    assert len(ts) == 6
    assert ts[0] == T0  # earliest chunk first, by first-message time


def test_metadata_capture(rec):
    cap = {"session_name": "s1", "serial": "GILABS-XX"}
    b = rec(capture=cap)
    b.add_camera("/ego/camera/0", indexed_frames(2))
    b.write()
    md = Session([b.path]).metadata
    assert md.capture["session_name"] == "s1"
    assert md.start_ns == T0


def test_imu_stream_clock(rec):
    b = rec()
    b.add_imu_bundle("/ego/imu/0/raw", T0, offsets=[0, 5_000_000, 10_000_000])
    b.write()
    samples = list(Session([b.path]).stream())
    assert len(samples) == 3
    assert all(isinstance(s, ImuSample) for s in samples)
    assert [s.t_ns for s in samples] == [T0, T0 + 5_000_000, T0 + 10_000_000]


def test_wide_imu_bundle_interleaves_monotonically(rec):
    # An IMU bundle spanning 200 ms (far wider than the 50 ms reorder window),
    # with camera frames falling inside its span. Output must stay t_ns-ordered.
    # Regression: the reorder watermark must follow message ARRIVAL, not the
    # bundle's expanded sample t_ns (which reach ~1 s past arrival on real ego).
    from visio_schema.reader import Frame

    b = rec()
    b.add_camera("/ego/camera/0", indexed_frames(6))  # t0 .. t0+5*33ms
    offsets = [i * 10_000_000 for i in range(21)]  # 0..200 ms, 21 samples
    b.add_imu_bundle("/ego/imu/0/raw", T0, offsets)
    b.write()
    els = list(Session([b.path]).stream())
    ts = [e.t_ns for e in els]
    assert ts == sorted(ts)  # monotonic across the wide bundle
    assert {type(e).__name__ for e in els} == {"Frame", "ImuSample"}
    # the frame at t0+66ms is interleaved (not stuck behind the whole bundle)
    assert any(isinstance(e, Frame) and e.t_ns == T0 + 2 * FRAME_DT for e in els)


def _calib_rec_with_imu(stereo_calib_rec, *, n_frames=6, n_bundles=1, span_ms=200,
                        step_ms=10):
    """A recording carrying BOTH calibration (so "auto" is non-identity) and IMU.

    ``n_bundles`` matters: with a single bundle emitted before every frame, its samples
    are the smallest keys in the stream and `_reorder`'s cutoff is never exercised. Real
    files bundle ~1 s of IMU per message INTERLEAVED with frames (alignment md 2), which
    is the shape that can release IMU ahead of the frames falling inside its span.
    """
    _path, b = stereo_calib_rec(n_frames=n_frames)
    offsets = [i * step_ms * 1_000_000 for i in range(span_ms // step_ms + 1)]
    for k in range(n_bundles):
        b.add_imu_bundle("/ego/imu/0/raw", T0 + k * span_ms * 1_000_000, offsets)
    return b.write()


def test_stream_applies_no_clock_correction(stereo_calib_rec):
    """ONE clock: `t_ns` is the wire stamp for every element kind.

    The recording carries a 39 ms `cam_imu_dt`, which the SDK used to subtract
    from every IMU sample. It must not: IMU sample time is exactly its bundle
    anchor plus `t_offset_ns`, and a frame's is exactly its `log_time`.
    """
    span_ms, step_ms = 200, 10
    path = _calib_rec_with_imu(stereo_calib_rec, span_ms=span_ms, step_ms=step_ms)
    sess = Session([path])
    assert sess.calibration.cam_imu_dt_ns == 39_000_000  # surfaced, NOT applied

    imu = [e for e in sess.stream() if isinstance(e, ImuSample)]
    assert imu, "fixture produced no IMU samples"
    # The fixture anchors its bundle at T0 with samples every step_ms, so the exact
    # absolute grid is reproducible here — any applied shift moves every one of them.
    expected = [T0 + i * step_ms * 1_000_000
                for i in range(span_ms // step_ms + 1)]
    assert [e.t_ns for e in imu] == expected


def test_elements_expose_exactly_one_timestamp():
    """No second stamp to choose between — the writer contract has nothing to get
    wrong. Guards against `pts_ns` / `capture_pts_ns` creeping back."""
    s = ImuSample("/imu", 42, gyro=np.zeros(3), accel=np.zeros(3))
    f = Frame("/cam", 42, np.zeros((2, 2), np.uint8))
    for el in (s, f):
        assert el.t_ns == 42
        assert not hasattr(el, "pts_ns")
        assert not hasattr(el, "capture_pts_ns")


def test_stream_stays_monotonic_under_wide_imu_bundles(stereo_calib_rec):
    """The _reorder invariant, which outlives the correction that complicated it.

    One IMU bundle expands to samples reaching far past its own arrival — here
    200 ms against a 50 ms reorder window — so the watermark must follow ARRIVAL,
    not the expanded sample times, or the bundle releases ahead of the camera
    frames that fall inside its span.
    """
    # interleaved wide bundles: without this shape the cutoff is never exercised
    path = _calib_rec_with_imu(stereo_calib_rec, n_frames=20, n_bundles=3,
                               span_ms=200, step_ms=10)
    els = list(Session([path]).stream())
    ts = [e.t_ns for e in els]
    assert ts == sorted(ts), "non-monotonic across the bundle spread"
    assert sum(isinstance(e, ImuSample) for e in els) == 3 * 21
    assert sum(not isinstance(e, ImuSample) for e in els) == 40  # 20 pairs


def test_topic_filter(stereo_calib_rec):
    path, _ = stereo_calib_rec(n_frames=3)
    frames = list(Session([path]).stream(topics=["/ego/camera/1"]))
    assert len(frames) == 3
    assert all(f.topic == "/ego/camera/1" for f in frames)


def test_canonical_sidecar_does_not_break_device_detection(tmp_path, rec):
    """A sidecar unioned into a device-prefixed recording must not blind the union.

    A derived stream writes CANONICAL topics, and `_detect_device` bails on the
    first non-prefixed topic it sees. Detecting over the union therefore returned
    None the moment any sidecar joined, after which every recording topic resolved
    as `/GILABS-…/ego/camera/0` and `stream(("/ego/camera/0",))` went silent — a
    stage reading its upstream's output would have seen an empty recording.

    The sidecar here is any canonical-topic MCAP; what matters is the NAMING, not
    which writer produced it.
    """
    b = rec(device="GILABS-AABBCCDD")
    b.add_camera("/ego/camera/0", indexed_frames(3))
    b.write()
    side = RecBuilder(tmp_path / "depth.mcap")          # device=None -> canonical
    side.add_camera("/ego/camera/0/depth", indexed_frames(3))
    side.write()

    s = Session([b.path, side.path])
    assert s.device == "GILABS-AABBCCDD", "the sidecar must not veto detection"
    assert {"/ego/camera/0", "/ego/camera/0/depth"} <= {t.topic for t in s.topics()}
    assert len(list(s.stream(("/ego/camera/0",)))) == 3


def test_two_devices_in_one_union_still_have_no_device(tmp_path):
    """The relaxation must not start picking one arbitrarily."""
    a = RecBuilder(tmp_path / "a.mcap", device="GILABS-AAAAAAAA")
    a.add_camera("/ego/camera/0", indexed_frames(2)).write()
    b = RecBuilder(tmp_path / "b.mcap", device="GILABS-BBBBBBBB")
    b.add_camera("/ego/camera/0", indexed_frames(2), t0=T0 + 10_000_000_000).write()
    assert Session([a.path, b.path]).device is None


def test_strip_prefix_passes_canonical_and_drops_a_foreign_device():
    from visio_schema.reader import strip_device_topic_prefix as strip

    dev = "GILABS-AABBCCDD"
    assert strip(f"/{dev}/ego/camera/0", dev) == "/ego/camera/0"
    # already canonical (what a sidecar writes) -> unchanged, NOT dropped
    assert strip("/ego/camera/0/depth", dev) == "/ego/camera/0/depth"
    # a different device's topic is still genuinely not ours
    assert strip("/GILABS-99999999/ego/camera/0", dev) is None
    assert strip("/ego/camera/0", None) == "/ego/camera/0"
