# Changelog

All notable wire-contract changes to `visio-schema`. Versioning follows
[`docs/protocol/versioning.md`](docs/protocol/versioning.md). Pre-1.0, breaking changes
bump the MINOR version.

## 0.6.3 — 2026-07-23

### Added `SetCalibration.camera_tuning` — per-unit ISP measurement (wire-compatible)

- **New `visio_schema.v1.calibration.CameraTuning` (`WbMeasurement` + `WbPoint`)**,
  carried on the `SetCalibration` artifact oneof at **tag 15**, `sensor_kind =
  CAMERA`. A lens+IR-cut varies unit to unit, so one per-model iqfile cannot be
  correct for every part; this is how a fixture tells a device what its own
  optics measured.
- **Set-only — the first `SetCalibration` artifact that is never re-published.**
  Nothing downstream consumes it, so it is persisted and applied but has no
  `/<dev>/...` topic. Consequence for callers: the `CommandResult` is the *only*
  acknowledgement, with no 1 Hz re-broadcast to confirm against.
- **Points are the only vocabulary.** There is no separate "apply this
  multiplier" field, because two ways of stating a correction can disagree and
  nothing would arbitrate. With `awb_mode = LIVE` a correct pipeline renders a
  neutral target at `rg = bg = 1.0` — that is what AWB is for — so a point's
  `rg` *is* the residual error, and a chosen ×1.0565 red gain is simply the point
  `rg = 1/1.0565`. A later fixture measurement then replaces it at the same CCT
  without the record changing shape. At least one point is required.
- **Carries measurements and nothing else.** The model that extends a
  measurement across colour temperature and the resulting ISP values live on the
  device, so improving either is an OTA rather than a re-push of every unit.
- **Indexed by CCT**, not by illuminant name or iqfile light-source slot, so
  changing a sensor's light-source list does not reinterpret stored records.
  `mired` is absent (derivable as `1e6/cct`; carrying both invites a record whose
  two indices disagree).
- **One record per unit, not per camera.** `sensor_index` is still required by
  `SetCalibration` but selects nothing here: the ISP shares a single AWB gain
  table across a rig's sensors, measured on an RV1106 stereo ego — a record
  naming `cam0` alone moved *both* cameras by the same factor, under camgroup and
  under per-sensor free-run alike. A per-camera artifact would have promised what
  the hardware cannot do.
- **`lens_model` / `lens_batch` are the only identity fields on the wire**,
  because the lens is the one thing no device can sense. `lens_model` is required;
  `lens_batch` is recorded and logged but **never gates**, since correcting a unit
  from a new lens batch is the entire purpose.
- **Deliberately NOT on the wire**: the sensor and the ISP tuning revision. A
  host tool can observe neither, so a value it sent would be an assertion about
  state it cannot see. The device stamps its own when it stores a record and
  re-checks at apply, catching a reflash between calibration and use.
- Field numbers 2-4 reserved on `CameraTuning` for lens-shading, black-level and
  defect-pixel artifacts.

## 0.6.2 — 2026-07-17

### `SetTime` carries the host GPS fix (wire-compatible)

- **New `SetTime` fields `latitude` (3) / `longitude` (4).** The boards have no
  GNSS receiver, so the phone's fix rides the same on-connect push as the wall
  clock. A non-zero fix is persisted into the recording-metadata sidecar (the
  same coordinates `SetRecordingMeta` carries) and stamped into every new
  session's metadata; 0 = no fix, the device keeps its stored coordinates.
- **Clarified `SetRecordingMeta.latitude/longitude` keep-on-zero semantics.**
  A 0 coordinate keeps the stored value instead of clearing it (text fields
  still clear on empty) — a host without a fix must not wipe the last known
  position.

## 0.6.1 — 2026-07-17

### Added `Command.reset_to_ap` (wire-compatible)

- **New Command body `ResetToAp` (tag 29, no fields).** Forgets the provisioned
  Wi-Fi STA credentials and returns the device to its setup soft-AP. A one-shot
  action, not a mode: the stored credentials are erased (no rejoin on the next
  boot), any STA association is dropped, and the AP comes back up.
- Like `ConnectWifi`, the result usually never reaches a caller on the STA link
  — the device tears that link down to switch radios (single-radio RTL8821CS),
  so a post-send transport drop means success, not failure.

## 0.6.0 — 2026-07-16

### Slimmed `ImuCalibration` to noise-model + sync only (BREAKING)

`ImuCalibration` now carries **only** what the system owns and a consumer's
filter needs: `accel_noise_density` (14), `gyro_noise_density` (16),
`update_rate_hz` (18), and `time_offset_to_cam0_s` (21). Everything else is
**removed and its tag reserved**:

- **Per-axis bias/scale (tags 1-12).** Scale is factory-trimmed per part and was
  only the redundant diagonal of the misalignment matrix; bias is a runtime
  *state* (re-randomized each power-up, drifting in-run), estimated online by the
  consumer's filter — never a stored constant. Nothing ever applied these.
- **Bias random walk (`accel_random_walk` 15, `gyro_random_walk` 17).** A
  stochastic noise strength that depends on the host board / bandwidth /
  vibration, not a per-unit constant — left to the consumer's process-noise
  default (or an in-situ Allan run). Not on any MEMS datasheet.
- **Scale-misalignment intrinsics (`accel_misalignment` 19, `gyro_misalignment`
  20, `gyro_g_sensitivity` 22, `gyro_to_accel_rotation` 23).** Reverts the
  0.5.x scale-misalignment work: for factory-trimmed parts the residual isn't
  worth storing, and the on-device store never applied it.

Removing fields trips `buf breaking` (`FIELD_NO_DELETE`); pre-1.0 that is a MINOR
bump per [`versioning.md`](docs/protocol/versioning.md). Wire impact is benign:
old readers decode the dropped scalars as their proto3 default (bias identity is
0 — harmless; scale isn't 1, but nothing multiplies by it), and the device's JSON
store ignores unknown/stale keys, so an old `calibration.json` still loads. Tags
1-13, 15, 17, 19, 20, 22, 23 are reserved so nothing reuses them.

## 0.5.2 — 2026-07-14

### Added `RecordingEntry.damaged` (wire-compatible)

- **New field `RecordingEntry.damaged` (tag 16, bool).** Marks a session whose
  non-active `.mcap` part lacks the end magic (truncated by a power cut /
  card removal mid-recording). Such parts are skipped by auto-upload; the app
  lists them separately with recovery guidance.

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
