"""Root-scoped physical calibration; no estimator tuning or clock correction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial.transform import Rotation

from .domain import CameraCalib


@dataclass(frozen=True)
class ImuInfo:
    rate_hz: float
    time_offset_to_cam0_s: float
    accel_noise_density: float
    gyro_noise_density: float


@dataclass(frozen=True, eq=False)
class Fiducial:
    dictionary: str
    marker_id: int
    size_m: float
    T_cam0_fiducial: np.ndarray


@dataclass
class RootCalibration:
    root: str
    cameras: dict[int, CameraCalib] = field(default_factory=dict)
    T_cam0_camera: dict[int, np.ndarray] = field(default_factory=dict)
    T_cam0_imu: dict[int, np.ndarray] = field(default_factory=dict)
    imus: dict[int, ImuInfo] = field(default_factory=dict)
    fiducials: tuple[Fiducial, ...] = ()
    camera_frames: dict[int, str] = field(default_factory=dict)
    imu_frames: dict[int, str] = field(default_factory=dict)


def pose_matrix(position, orientation) -> np.ndarray:
    p = np.array([position.x, position.y, position.z], dtype=float)
    q = np.array([orientation.x, orientation.y, orientation.z, orientation.w])
    if not np.all(np.isfinite(p)) or not np.all(np.isfinite(q)):
        raise ValueError("calibration pose contains nonfinite values")
    if abs(np.linalg.norm(q) - 1.0) > 1e-5:
        raise ValueError("calibration orientation must be a unit quaternion")
    T = np.eye(4)
    T[:3, :3] = Rotation.from_quat(q).as_matrix()
    T[:3, 3] = p
    return T


def parse_fiducials(bundle) -> tuple[Fiducial, ...]:
    result = []
    seen = set()
    for m in bundle.fiducials:
        key = (m.dictionary, int(m.marker_id))
        if not m.dictionary or key in seen:
            raise ValueError(f"invalid or duplicate fiducial identity: {key}")
        if not np.isfinite(m.size_m) or m.size_m <= 0:
            raise ValueError(f"invalid fiducial size: {key}")
        if not m.HasField("T_cam0_fiducial"):
            raise ValueError(f"missing T_cam0_fiducial: {key}")
        pose = m.T_cam0_fiducial
        if not pose.HasField("position") or not pose.HasField("orientation"):
            raise ValueError(f"missing complete fiducial pose: {key}")
        result.append(Fiducial(*key, m.size_m, pose_matrix(pose.position, pose.orientation)))
        seen.add(key)
    return tuple(result)


def read_calibrations(rows) -> dict[str, RootCalibration]:
    """Read canonical (schema, topic, proto) rows supplied by Session."""
    from .session import _parse_camera_calib

    roots: dict[str, RootCalibration] = {}
    pattern = re.compile(r"^(.*)/(camera|imu)/(\d+)/(intrinsics|extrinsics|info)$")
    for _schema, topic, msg in rows:
        if topic is None:
            continue
        if topic.endswith("/fiducials"):
            root = topic[: -len("/fiducials")]
            roots.setdefault(root, RootCalibration(root)).fiducials = parse_fiducials(msg)
            continue
        match = pattern.fullmatch(topic)
        if match is None:
            continue
        root, kind, index, artifact = match.groups()
        index = int(index)
        cal = roots.setdefault(root, RootCalibration(root))
        if kind == "camera" and artifact == "intrinsics":
            cal.cameras[index] = _parse_camera_calib(topic, msg)
            cal.camera_frames[index] = msg.frame_id
            if index == 0:
                cal.T_cam0_camera[0] = np.eye(4)
        elif artifact == "extrinsics":
            if any(
                frame.startswith("/") and frame != f"{root}/{suffix}"
                for frame, suffix in (
                    (msg.parent_frame_id, "cam0"),
                    (msg.child_frame_id, f"cam{index}" if kind == "camera" else f"imu{index}"),
                )
            ):
                raise ValueError(f"{topic}: transform belongs to a different root")
            parent = msg.parent_frame_id.rsplit("/", 1)[-1]
            child = msg.child_frame_id.rsplit("/", 1)[-1]
            expected = f"cam{index}" if kind == "camera" else f"imu{index}"
            if parent != "cam0" or child != expected:
                raise ValueError(f"{topic}: expected cam0 <- {expected}, got {parent} <- {child}")
            T = pose_matrix(msg.translation, msg.rotation)
            table = cal.T_cam0_camera if kind == "camera" else cal.T_cam0_imu
            table[index] = T
            frames = cal.camera_frames if kind == "camera" else cal.imu_frames
            frames[index] = msg.child_frame_id
        elif kind == "imu" and artifact == "info":
            cal.imus[index] = ImuInfo(
                msg.update_rate_hz,
                msg.time_offset_to_cam0_s,
                msg.accel_noise_density,
                msg.gyro_noise_density,
            )
    return roots
