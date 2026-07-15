"""Round-trip tests for the calibration schema: the extended ImuCalibration and
the SetCalibration command artifact (camera intrinsics / extrinsics / imu_info)."""
from __future__ import annotations

from visio_schema.foxglove import CameraCalibration_pb2, FrameTransform_pb2
from visio_schema.v1.calibration import imu_pb2
from visio_schema.v1.control import command_pb2
from visio_schema.wire import schema


def test_imu_calibration_noise_roundtrip():
    """ImuCalibration carries bias/scale + the kalibr noise model + time offset."""
    c = imu_pb2.ImuCalibration(
        accel_bias_x=0.1, gyro_scale_z=1.01,
        accel_noise_density=0.008, accel_random_walk=0.0004,
        gyro_noise_density=0.0009, gyro_random_walk=2.0e-5,
        update_rate_hz=200.0, time_offset_to_cam0_s=-0.0123,
        accel_misalignment=[1, 0, 0, 0, 1, 0, 0, 0, 1],
    )
    out = imu_pb2.ImuCalibration.FromString(c.SerializeToString())
    assert out.accel_noise_density == 0.008
    assert out.gyro_random_walk == 2.0e-5
    assert out.time_offset_to_cam0_s == -0.0123
    assert list(out.accel_misalignment) == [1, 0, 0, 0, 1, 0, 0, 0, 1]


def test_imu_calibration_intrinsics_roundtrip():
    """The extended kalibr scale-misalignment intrinsics: accel/gyro misalignment
    (M), gyro g-sensitivity (A), and the gyro→accel rotation (C_gyro_i)."""
    c = imu_pb2.ImuCalibration(
        accel_misalignment=[1.002, 0.001, 0, 0, 0.999, 0.002, 0, 0, 1.001],
        gyro_misalignment=[0.998, 0, 0.003, 0, 1.001, 0, -0.001, 0, 1.0],
        gyro_g_sensitivity=[1e-4, 0, 0, 0, -2e-4, 0, 0, 0, 3e-4],
        gyro_to_accel_rotation=[1, 0, 0, 0, 1, 0, 0, 0, 1],
    )
    out = imu_pb2.ImuCalibration.FromString(c.SerializeToString())
    assert list(out.gyro_g_sensitivity) == [1e-4, 0, 0, 0, -2e-4, 0, 0, 0, 3e-4]
    assert list(out.gyro_to_accel_rotation) == [1, 0, 0, 0, 1, 0, 0, 0, 1]
    assert list(out.gyro_misalignment)[2] == 0.003


def test_imu_calibration_mounting_pose_removed():
    """mounting_pose was retired (field 13 reserved) — it's no longer a field."""
    assert "mounting_pose" not in imu_pb2.ImuCalibration.DESCRIPTOR.fields_by_name


def test_set_calibration_camera_intrinsics_roundtrip():
    """SetCalibration carries foxglove.CameraCalibration for camera intrinsics."""
    intr = CameraCalibration_pb2.CameraCalibration(
        width=1920, height=1080, frame_id="cam0",
        distortion_model="kannala_brandt",
        D=[0.0035, 0.0007, -0.0021, 0.0002],
        K=[190.9, 0, 254.9, 0, 190.9, 256.9, 0, 0, 1],
    )
    cmd = command_pb2.Command(
        target_device="ego", command_id=7,
        set_calibration=command_pb2.SetCalibration(
            sensor_kind=command_pb2.SetCalibration.CAMERA,
            sensor_index=0, intrinsics=intr,
        ),
    )
    out = command_pb2.Command.FromString(cmd.SerializeToString())
    assert out.WhichOneof("body") == "set_calibration"
    sc = out.set_calibration
    assert sc.sensor_kind == command_pb2.SetCalibration.CAMERA
    assert sc.WhichOneof("artifact") == "intrinsics"
    assert sc.intrinsics.distortion_model == "kannala_brandt"
    assert list(sc.intrinsics.D) == [0.0035, 0.0007, -0.0021, 0.0002]


def test_set_calibration_extrinsics_roundtrip():
    """SetCalibration carries a single foxglove.FrameTransform anchored to cam0."""
    tf = FrameTransform_pb2.FrameTransform(parent_frame_id="cam0", child_frame_id="imu0")
    tf.translation.x = 0.01
    tf.rotation.w = 1.0
    cmd = command_pb2.Command(
        set_calibration=command_pb2.SetCalibration(
            sensor_kind=command_pb2.SetCalibration.IMU, sensor_index=0, extrinsics=tf,
        ),
    )
    sc = command_pb2.Command.FromString(cmd.SerializeToString()).set_calibration
    assert sc.WhichOneof("artifact") == "extrinsics"
    assert sc.extrinsics.parent_frame_id == "cam0"
    assert sc.extrinsics.child_frame_id == "imu0"
    assert sc.extrinsics.translation.x == 0.01


def test_set_calibration_imu_info_roundtrip():
    """SetCalibration carries ImuCalibration as the imu_info artifact."""
    cmd = command_pb2.Command(
        set_calibration=command_pb2.SetCalibration(
            sensor_kind=command_pb2.SetCalibration.IMU, sensor_index=0,
            imu_info=imu_pb2.ImuCalibration(gyro_noise_density=0.0009),
        ),
    )
    sc = command_pb2.Command.FromString(cmd.SerializeToString()).set_calibration
    assert sc.WhichOneof("artifact") == "imu_info"
    assert sc.imu_info.gyro_noise_density == 0.0009


def test_calibration_payloads_announceable():
    """The published calibration types resolve in the schema pool (announce-ready)."""
    for name in ("foxglove.CameraCalibration", "foxglove.FrameTransform",
                 "visio_schema.v1.calibration.ImuCalibration"):
        assert len(schema.file_descriptor_set(name)) > 0
