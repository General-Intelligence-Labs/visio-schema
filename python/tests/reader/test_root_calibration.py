import numpy as np
import pytest
from _helpers import RecBuilder

from visio_schema.foxglove.CameraCalibration_pb2 import CameraCalibration
from visio_schema.foxglove.FrameTransform_pb2 import FrameTransform
from visio_schema.reader import Session
from visio_schema.v1.calibration.fiducial_pb2 import FiducialCalibration
from visio_schema.v1.calibration.imu_pb2 import ImuCalibration
from visio_schema.v1.control.command_pb2 import SetCalibration


def test_root_preservation_calibration_and_bundle_roundtrip(tmp_path):
    b = RecBuilder(tmp_path / "rig.mcap")
    for root in ("/GILABS-one/ego", "/GILABS-two/gripper_left"):
        cam = CameraCalibration(width=64, height=48, distortion_model="equidistant",
                                K=[100., 0., 32., 0., 100., 24., 0., 0., 1.],
                                D=[0., 0., 0., 0.])
        imu = ImuCalibration(update_rate_hz=200., time_offset_to_cam0_s=.012)
        ft = FrameTransform(parent_frame_id=root+"/cam0", child_frame_id=root+"/imu0")
        ft.rotation.w = 1.
        ft.translation.x = .05
        bundle = FiducialCalibration()
        m = bundle.fiducials.add(dictionary="DICT_4X4_50", marker_id=7, size_m=.0225)
        m.T_cam0_fiducial.orientation.w = 1.
        m.T_cam0_fiducial.position.z = .1
        for topic, msg in [(root+"/camera/0/intrinsics", cam),
                           (root+"/imu/0/info", imu),
                           (root+"/imu/0/extrinsics", ft),
                           (root+"/fiducials", bundle)]:
            b._rows.append((topic, msg.DESCRIPTOR.full_name, 1, msg.SerializeToString()))
    path = b.write()
    session = Session.open(path, topic_mode="preserve")
    assert set(session.calibrations) == {"/GILABS-one/ego", "/GILABS-two/gripper_left"}
    for cal in session.calibrations.values():
        assert np.allclose(cal.T_cam0_camera[0], np.eye(4))
        assert cal.T_cam0_imu[0][0, 3] == .05
        assert cal.imus[0].time_offset_to_cam0_s == .012
        assert cal.fiducials[0].marker_id == 7
        assert cal.fiducials[0].T_cam0_fiducial[2, 3] == .1
    sc = SetCalibration(sensor_kind=SetCalibration.UNIT, fiducials=bundle)
    assert SetCalibration.FromString(sc.SerializeToString()).WhichOneof("artifact") == "fiducials"


def test_reader_stream_never_preloads_frame_info(tmp_path, monkeypatch):
    from _helpers import indexed_frames

    from visio_schema.reader import Frame
    b = RecBuilder(tmp_path/"r.mcap")
    b.add_camera("/ego/camera/0", indexed_frames(5))
    b.add_frame_info("/ego/camera/0", n=5, drop=(2,))
    s = Session.open(b.write())
    monkeypatch.setattr(s, "_exposure_tracks", lambda: pytest.fail("metadata prepass"))
    frames = list(s.stream(["/ego/camera/0"]))
    assert len(frames) == 5 and all(isinstance(f, Frame) for f in frames)
    assert [f.exposure.interpolated for f in frames] == [False, False, True, False, False]


def test_bad_fiducial_and_transform_are_rejected():
    from visio_schema.reader.calibration import parse_fiducials, read_calibrations
    bundle = FiducialCalibration()
    bundle.fiducials.add(dictionary="DICT_4X4_50", marker_id=1, size_m=.02)
    with pytest.raises(ValueError, match="missing T_cam0"):
        parse_fiducials(bundle)
    ft = FrameTransform(parent_frame_id="imu0", child_frame_id="cam0")
    ft.rotation.w = 1.
    with pytest.raises(ValueError, match="expected cam0"):
        read_calibrations([(ft.DESCRIPTOR.full_name, "/ego/imu/0/extrinsics", ft)])
