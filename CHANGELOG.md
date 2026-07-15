# Changelog

All notable wire-contract changes to `visio-schema`. Versioning follows
[`docs/protocol/versioning.md`](docs/protocol/versioning.md). Pre-1.0, breaking changes
bump the MINOR version.

## 0.5.3 — 2026-07-15

### Added extended IMU intrinsics to `ImuCalibration` (wire-compatible)

- **New `ImuCalibration.gyro_g_sensitivity` (tag 22, `repeated double`, row-major
  3×3).** Gyroscope g-sensitivity — linear acceleration leaking into the gyro
  output (kalibr `gyroscopes.A`, units (rad/s)/(m/s²)). Empty = none.
- **New `ImuCalibration.gyro_to_accel_rotation` (tag 23, `repeated double`,
  row-major 3×3).** Rotation of the gyro triad into the accel/IMU frame (kalibr
  `gyroscopes.C_gyro_i`). Empty = identity.
- Completes the `kalibr_calibrate_imu_camera --imu-models scale-misalignment`
  output on the wire: accel/gyro scale+misalignment already rode on
  `accel_misalignment`/`gyro_misalignment` (tags 19/20); these add the remaining
  two matrices. Stored + reported by the device; not yet applied to samples.

## 0.5.1 — 2026-07-14

### Added `Command.set_resolution` (wire-compatible)

- **New Command body `SetResolution` (tag 28, `width`/`height` uint32).**
  Persists the camera capture resolution (all cameras) to a device-side
  sidecar; takes effect on the next boot, like `SetBitrate`. Unsupported
  geometries snap to the sensor's default mode at boot.
- **New `DeviceState.video_width`/`video_height` (tags 27/28).** Echo the
  persisted resolution the same way `video_bitrate_kbps` echoes bitrate.

## 0.5.0 — 2026-07-14

### Merged main (S3/OSS auto-upload) into dev — `SetNoticeLang` retagged (BREAKING vs 0.4.2)

- **`Command.set_auto_upload` (tag 26, `SetAutoUpload`)** and the S3/OSS
  auto-upload surface from main (`CommandResult.storage_access_key_id`,
  MCAP capture-meta record) are now on dev. Tag 26 is owned by the shipped
  `SetAutoUpload`.
- **BREAKING: `Command.set_notice_lang` moved tag 26 → 27.** 0.4.2 assigned
  `SetNoticeLang` tag 26, colliding with main's released `SetAutoUpload`.
  Voice notices only exist on dev firmware, which must be rebuilt against
  this version; shipped (main-line) devices are unaffected.

## 0.4.2 — 2026-07-10

### Added `Command.set_notice_lang` (wire-compatible)

- **New Command body `SetNoticeLang` (tag 27, `lang` string; moved from 26
  when merging main, whose shipped `SetAutoUpload` owns tag 26).** Selects the
  language of a device's spoken voice notices (boards with a speaker). Applied
  immediately and persisted device-side; unknown languages fall back to the
  device default (English). Speakerless boards accept and ignore it. Sent by
  the companion app with the phone locale after connecting.

### Launcher host-side video decode (no wire-contract change)

- **`visio-display --serve` now decodes the device's H.265 on the host** so browsers that
  can't render HEVC (e.g. Edge/Chrome on Windows without the HEVC extension) still show
  video. It auto-detects a GPU decoder (D3D11VA/DXVA2/VideoToolbox/NVDEC/QSV/VAAPI/…) with
  a slice-threaded software fallback and re-encodes each frame to JPEG on per-camera worker
  threads, off the transport reader. Chain: browser WebCodecs H.265 → PyAV hardware decode
  + JPEG → PyAV software decode + JPEG.
- **Honest "slow video" UI.** Host-side decode — hardware *or* software — is not real-time,
  so the page flags it in red while transcoding and points to a plain, per-OS guide to
  install the browser's native HEVC support (Windows → the free Microsoft Store HEVC Video
  Extensions) for smooth, live video. en + zh.
- **Free-port launcher** — the server always auto-picks free WebSocket/HTTP ports, so a new
  launch never collides with a stale one on a fixed port.

No `.proto`/schema change from the launcher work — existing readers are unaffected.

## 0.4.1 — 2026-07-07

### Added `DeviceInfo.equipment_type` (wire-compatible)

- **New field `DeviceInfo.equipment_type` (tag 7, `string`).** Carries the device's
  logical role — `ego`, `glove_left`, `glove_right`, `gripper_left`, `gripper_right` —
  the leading topic segment for its channels. This was formerly implicit in
  `device_name`, but the Visio firmware repurposed `device_name` to the per-unit
  `GILABS-<code8>` label (the addressable name the app shows / targets via
  `Command.target_device` + OTA), leaving no explicit field for the role. `equipment_type`
  restores it: hubs forward it end-to-end alongside the other identity metadata.
- Threaded through the C++ `ChannelRegistry` (`DeviceView`, `Encode`/`Decode`,
  `SetMetadata`, `SelfInfo`) and the Python registry (`ChannelRegistry(... , equipment_type=...)`,
  `self_info()`). Empty by default, so a device that omits it is unchanged on the wire.

New optional field with a new tag number — wire-compatible in both directions, so a
**PATCH** bump per [`versioning.md`](docs/protocol/versioning.md). Old peers ignore the
field; new peers read empty when it is absent.

## 0.4.0 — 2026-07-07

### Removed cross-device exposure-grid sync (breaking)

- **Removed `service/exposure_sync/ExposureGrid`** and its `.proto`. Cross-device
  exposure alignment no longer rides a published/relayed grid; each device now follows a
  statically-configured phase on its heartbeat-synced clock independently, so no wire
  message is needed. (Added in 0.3.0; had no production users.)
- **Reserved `CONTROL_STREAM_EXPOSURE_SYNC = 6`** (number + name) in `wire.ControlStream`,
  mirroring the retired-`TIMESYNC` precedent. `FIRST_DYNAMIC` and all other ids are
  unchanged. `EXPOSURE_SYNC` is dropped from the `visio_schema.wire.control` facade and
  from `LINK_LOCAL_CONTROL`.

Deleting the `ExposureGrid` message trips `buf breaking` (message removal); pre-1.0 that
is a MINOR bump per [`versioning.md`](docs/protocol/versioning.md). Old peers never emitted
the stream, so nothing on the wire changes for them.

## 0.3.2 — 2026-07-04

### Launcher UX (no wire-contract change)

- **`visio-display --serve` "Current settings" auto-refresh** — the DeviceState header now
  polls `GetState` on a timer (DeviceState is pull-only, not streamed), so it stays live
  without the manual Refresh button, which is removed. The editable form fields are left
  untouched by the refresh so an in-progress edit isn't clobbered.

## 0.3.1 — 2026-07-04

### Tooling + fixes (no wire-contract change)

- **`visio-display --serve` device config** — configure a discovered device from the
  launcher: Wi-Fi (scanned host-side, then provisioned to the device), set clock, camera
  bitrate, default recording metadata, identify, and format SD; plus a current-state
  header. Config commands ride the same bidirectional connection as the live stream, with
  a Windows-compatible endpoint.
- **Recording MCAP parts open `O_CLOEXEC`** so their fds don't leak into forked child
  processes (fixes SD reformat aborting with the card still busy).

No `.proto`/schema change — existing readers are unaffected.

## 0.3.0 — 2026-07-02

### Cross-device exposure-grid sync (additive)

- **`CONTROL_STREAM_EXPOSURE_SYNC = 6`** (link-scoped) — new control stream for
  aligning camera exposures across hub-connected devices.
- **`service/exposure_sync/ExposureGrid`** — `{anchor_mono_ns (in hub clock),
  period_ns, source_device}`. A hub-connected group locks each device's exposures
  onto a shared periodic grid; one device is the source/master, the rest follow.
  `source_device` is bounded (`max_size:32`) so it decodes into a static struct.

New enum value + new `.proto` + new message type; existing readers ignore them, so
this is non-breaking (MINOR).

## 0.2.2 — 2026-07-02

### `FormatStorage` command (additive, wire-compatible)

- **`Command.format_storage = 25`** (`FormatStorage { string fs_type = 1; }`) —
  reformat + re-mount the recording SD card over the bus, for sealed units with
  no shell (manufacturing quality-check). `fs_type` empty = preserve the card's
  current filesystem (ext4/exfat/vfat); explicit type forces one. Answered by a
  `CommandResult` (ok + `DeviceState`).

## 0.2.1 — 2026-06-22

### `SystemHealth.realtime` wall-clock field (additive)

- **`SystemHealth.realtime = 9`** — device wall-clock timestamp, so consumers can
  read the board's real time (RV1106 boots to 1970 until SetTime).

### Camera bitrate control (additive, wire-compatible)

- **`SetBitrate` command** (`Command.set_bitrate = 24`) — sets the camera H.265
  target bitrate (kbit/s) for all cameras. Like `SetAutoStart`, the device
  persists it and applies it on the next boot.
- **`DeviceState.video_bitrate_kbps = 23`** — echoes the persisted bitrate so a
  client can show the active value.

Both are additive (new oneof body + new field); existing readers ignore them, so
this is non-breaking.

## 0.2.0 — 2026-06-20

### Packaging & tooling

- **PyPI packaging.** `visio-schema` now builds as a proper sdist + per-version
  wheels and publishes to PyPI on a `visio-schema-v*` tag via Trusted Publishing
  (`.github/workflows/wheels.yml`). Added `make sdist` / `make dist`, project
  metadata (readme, classifiers, URLs), a `py.typed` marker, and `MANIFEST.in`.
  See [`docs/publishing.md`](docs/publishing.md).
- **`visio-display` command.** The live viewer moved from `examples/python/` into
  the package (`visio_schema.display`) and installs as the `visio-display` console
  script (also `python -m visio_schema.display`).
- **One default install — no feature extras.** MCAP read/write and the viewer's
  dependencies (serial, Foxglove, Rerun, H.265 decode) are now base dependencies,
  so `pip install visio-schema` is all you need; the former `mcap` / `display`
  extras are gone.

### Timesync folded into the heartbeat beacon

- **Removed the standalone timesync exchange** (`timesync.v1` package and
  its dedicated stream). The NTP-style exchange now rides the heartbeat
  beacon on the hop-local `CONTROL_STREAM_HEARTBEAT` control stream — one
  message does both liveness and clock-offset estimation.
- **`Heartbeat` gains the beacon fields**: `tx_mono_ns` (1),
  `echo_tx_mono_ns` (2), `echo_rx_mono_ns` (3); `queue_depth` is now (4).
  An initiating beacon carries only `tx_mono_ns`; a responder replies
  immediately, echoing the peer's send and stamping its receive time. The
  initiator closes the loop with a min-RTT-filtered midpoint estimate.
  See [`docs/protocol/timesync.md`](docs/protocol/timesync.md).
- Peers are keyed by the **endpoint a beacon arrives on** (control streams
  are hop-local; the wire Header has no device field).

> Part of the broader wire redesign in this version (static `StreamKind`
> enum + `DeviceClass` addressing → dynamic `stream_id` + `ControlStream`
> + Foxglove-style channel discovery). That redesign is documented
> separately; this entry covers only the timesync→heartbeat merge.
