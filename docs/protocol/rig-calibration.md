# Rigid calibration across boards

A USB topology does not define rigidity. A manufacturing recipe binds physical
member UIDs and camera indices to one rigid solve. Ego2 Pro contains four head
cameras and two rigid stereo-addon cameras; its IMU belongs to the head. Moving
hand devices are separate rigid groups.

Sensor indices in `SetCalibration` and published calibration remain local to the
physical board. Each board stores geometry anchored at its own camera 0. A joint
solve is split and rebased before it is sent; solver camera 4 is not hardware
camera 4 on the addon.

`calibration.AssemblyCalibration` carries `result_id`, `head_uid`, `addon_uid` and
`t_head_cam0_addon_cam0` (a `foxglove.Pose`). Its transform is named
`T_head_cam0_addon_cam0`: it maps addon-camera-0 coordinates into head-camera-0
coordinates. Both members publish the same record on `/<root>/assembly`.
The `SetCalibration.assembly` artifact requires `UNIT`, index 0. An empty message
explicitly invalidates a previous assembly record.

The station invalidates previous membership, writes native board-local artifacts,
and verifies values reported by both boards before committing matching assembly
records. A sensor geometry update invalidates that board's assembly record.
There is no cross-board atomic commit: consumers must require both matching
records, the exact bound member UIDs, and the expected solution identifier. A
missing or differing record is an incomplete calibration.

For multi-board recordings, payload timestamps remain producer-local. The native
bus rewrites `Header.timestamp` into the receiving clock domain and MCAP stores
that as `log_time`. Joint calibration conversion uses this header timeline for
camera frames and for the first sample of an IMU bundle, then applies each
sample's acquisition offset. It must not independently synchronize streams or
pair timestamps from unrelated producer clocks.
