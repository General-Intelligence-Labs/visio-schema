# Rigid calibration across boards

A USB topology does not define rigidity. A manufacturing recipe binds physical
member UIDs and camera indices to one rigid solve. Ego2 Pro contains four head
cameras and two rigid stereo-addon cameras; its IMU belongs to the head. Moving
hand devices are separate rigid groups.

Sensor indices in `SetCalibration` and published calibration remain local to the
physical board. Each board stores geometry anchored at its own camera 0. A joint
solve is split and rebased before it is sent; solver camera 4 is not hardware
camera 4 on the addon.

The addon alone stores the pose of its camera 0 in the head camera 0 frame.
It uses the existing `SetCalibration` command, `sensor_kind=CAMERA`,
`sensor_index=0`, `artifact.extrinsics=foxglove.FrameTransform`:

- Topic: `/<addon root>/camera/0/extrinsics` (normally
  `/stereo_addon/camera/0/extrinsics`), reported at 1 Hz.
- `parent_frame_id`: `<head serial>/cam0`.
- `child_frame_id`: `<addon serial>/cam0`.
- Direction: `p_head0 = R * p_addon0 + t`; translation is in metres.

Camera 0 remains the board-local anchor. Its optional external pose requires
distinct, fully qualified cam0 frames; a camera0 self-transform is rejected.
An empty camera0 transform explicitly clears the external pose. Camera1 and
other local sensors retain their existing `cam0`-relative frame IDs and topics.
Consumers compose `T_head0_addon0 * T_addon0_addon1` for addon camera1. They must
check that the physical serials in the transform match the connected boards.
The head stores no duplicate copy, and there is no assembly-result identifier.

Clients write native camera artifacts and verify fresh values on the same
published topics. These per-artifact writes are not atomic across boards.
The companion retains original poses on the phone before saving, checks fresh
geometry and identity, and restores interrupted writes before a new sweep.
Factory tooling clears the addon anchor before a joint solve, writes and
verifies board-local artifacts, then publishes the external anchor last.
Target profile, print confirmation and validation evidence remain client data,
not protocol fields.

For multi-board recordings, payload timestamps remain producer-local. The native
bus rewrites `Header.timestamp` into the receiving clock domain and MCAP stores
that as `log_time`. Joint calibration conversion uses this header timeline for
camera frames and for the first sample of an IMU bundle, then applies each
sample's acquisition offset. It must not independently synchronize streams or
pair timestamps from unrelated producer clocks.
