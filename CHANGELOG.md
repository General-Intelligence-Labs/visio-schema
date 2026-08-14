# Changelog

All notable wire-contract changes to `visio-schema`. Versioning follows
[`docs/protocol/versioning.md`](docs/protocol/versioning.md). Pre-1.0, breaking changes
bump the MINOR version.

## 0.8.0 — 2026-08-14

### Added `visio_schema.reader` — the element layer over `read_mcap` / `read_serial`

Both row sources already yield the same `(Message, Channel)` shape. This adds the
layer directly above them, turning rows into **elements**: decoded, unbundled,
clock-normalized values that share one interface.

    rows       (Message, Channel)          read_mcap / read_serial
    elements   Frame | ImuSample | Record  Session.stream

`Session` merges a recording's chunks and any sidecars onto one timeline with a
reorder window measured from the chunk seams; `sync` / `resample` / `prefetch`
reshape that single stream with bounded state and an explicit tolerance.
`Element.t_ns` IS the wire stamp — no read-time correction is applied here.

Ported wholesale from visio-post-processing, which now consumes it, and verified
**byte-identical** on a real ego session: the element stream, calibration and
keyframe cadence hash the same before and after across 88,799 elements.

Not exported from the `visio_schema` facade — import it explicitly as
`visio_schema.reader`. The frozen public API is unchanged, so the surface can
still settle before it is pinned.

New: a **registered adapter table** maps a wire schema to element(s), replacing a
fixed if/elif dispatch. `Session.open(..., adapters={schema: factory})` adds typed
elements for one session only; registering globally would retype every existing
consumer's elements, which breaks readers that assert they receive a `Record`.

### Added `McapWriter.add_metadata` and `Message.stamped`

`add_metadata(name, kv)` writes an MCAP metadata record — where provenance
belongs, readable without decoding a message — and re-emits it into every rolled
part so each part stands alone. `Message.stamped(payload, t_ns, *, seq)` is the
write-side mirror of `message_class`. `McapWriter` also takes optional `profile`
and `library`; both default to what it already produced, so existing output is
unchanged.

### Added `visio_schema.build` — foxglove message builders

`pose_in_frame` · `compressed_video` · `compressed_image` · `raw_image_mono16` ·
`camera_calibration` · `frame_transform`. Values in, protobuf message out, so the
same builder serves an MCAP write and a live bus send. Lives at the package root
because `visio_schema/foxglove/` is generated and gitignored.

### `av` is now pinned exactly (`av==12.3.0`), not floored

**Behaviour-affecting.** A different PyAV decodes a different H.265 frame set:
measured on a real ego session, 12.3.0 vs 17.0.0 give identical element counts,
identical topics and an identical undecoded pass, but different pixels in every
decode mode. Anything comparing two runs is void if they used different PyAVs.
The decoder also warns at runtime when the installed version differs, and
`tests/reader/test_av_pin.py` keeps that constant in step with this pin.

New extras: `[reader]` (scipy, for the one lazy import in the extrinsics parse)
and `[gpu]` (cupy + PyNvVideoCodec for NVDEC). `numpy` becomes a base dependency.
The viewer/MCAP surface stays in the default install.

## 0.7.3 — 2026-08-06

### Added `Command.set_notice_volume` (tag 38) + `DeviceState.notice_volume` (tag 34)

App-facing loudness control for the voice notices a speaker-equipped board plays,
0-100. Persisted device-side, applied without a reboot; every notice is affected —
announcements, errors, and the recording heartbeat alike. 0 is a true mute: the
codec's gain floor is quiet but not silent, so the device skips playback instead.

`DeviceState.notice_volume` is `optional` for the same reason the toggles are
tri-state: absence covers speakerless boards and pre-volume firmware, so the app
hides the control instead of rendering one it cannot move — and 0 could not be the
sentinel here because it is a legal value (mute) as well as the proto3 default.
Purely additive; ships with the matching device firmware change.

## 0.7.2 — 2026-08-04

### Added `SetRecordingMeta.operator_id` (tag 9) + `SetRecordingMeta.environment_id` (tag 10)

Fleet identifiers — which operator account and which environment/site a rig is
capturing under — persisted device-side beside the existing recording-metadata
defaults and stamped into every new session's `session.json` and `visio.capture` MCAP
record. Nothing user-facing sets them: provisioning tooling and fleet scripts do, over
this same command.

Both are `optional` (proto3 presence), and that is the point rather than a style
choice. A client sends `SetRecordingMeta` whole and the device replaces the stored
text with what arrives, so a plain field would be cleared by every app or device-web
"Set" — wiping an id no UI shows and nobody there can restore. **Absent keeps the
stored value; present replaces it; present-but-empty clears it.** Existing clients
send neither and so can no longer clobber them.

Purely additive: an old device ignores both tags, and an old client's messages decode
unchanged. Ships with the matching firmware producer change.

## 0.7.1 — 2026-08-03

### Added `Command.set_recording_heartbeat` (tag 37) + `DeviceState.recording_heartbeat` (tag 33)

App-facing toggle for the tick-tock voice heartbeat a speaker-equipped board plays
while recording. Persisted device-side, applied without a reboot; only the heartbeat
is affected — start/stop announcements and error notices keep playing.

`DeviceState.recording_heartbeat` is tri-state (like `audio_recording` /
`status_report`): `UNSUPPORTED` covers speakerless boards and pre-toggle firmware, so
the app hides the switch instead of rendering one it cannot move. Purely additive;
ships with the matching device firmware change.

### Added `visio_schema.v1.sensor.SystemHealth.disk_total_bytes` (tag 10)

Total size of the recording volume — the denominator that `disk_free_bytes` (tag 6) has
always lacked. A consumer holding only the free byte count cannot say how full the card
is; the app's storage row wants "N of M free", and `DeviceState.disk_free_pct` carries
only a rounded percentage.

Both disk fields are emitted only when there is a real recording medium to measure — a
device whose recording root has degraded to its own rootfs (card yanked, or never
present) sends neither, rather than reporting the rootfs's geometry as the card's.

Purely additive: an old consumer ignores tag 10, and a new consumer treats an absent
`disk_total_bytes` exactly as it already treats an absent `disk_free_bytes`. Ships with
the matching firmware producer change.

### Changed (docs) `TestStorage` — credential probe is a list, not a HEAD

No wire change — the comment now matches what the device does. It validates storage
credentials with a `max-keys=1` list under the recordings prefix instead of a HEAD of
the bucket, so the key needs only put + list grants (on Aliyun OSS: no
`oss:GetBucketInfo`), and the probe exercises exactly the grant the app's cloud
recordings list depends on.

**The required grant changed with it**, which the "no wire change" above does not cover:
a key minted for the new probe has no `oss:GetBucketInfo`, so firmware older than this
release — which HEADs the bucket — fails its storage Test against that key. Re-issuing a
fleet's device policy is therefore coupled to its firmware version.

## 0.7.0 — 2026-08-01

### Changed (BREAKING) `visio_schema.v1.sensor.CameraFrameInfo` — one join key

Trimmed to a single identifier contract. `timestamp` is now the **only** join key and is
unique per stream *by construction*; `isp_frame_id` survives as a diagnostic only.

- **Removed `frame_id` (tag 2)** — a coordinate frame NAME (the ROS/foxglove
  `Header.frame_id` convention), copied from the sibling video topic. Exposure and gains
  are not expressed in any coordinate frame, so it anchored nothing, and its similarity to
  `isp_frame_id` (a counter) actively misled. Number and name reserved.
- **Removed `vi_time_ref` (tag 4)** — the drain frame's counter, published so
  `vi_time_ref != isp_frame_id` could flag a substitute-stamped entry. The producer no
  longer substitute-stamps at all, and on real recordings that flag was aliased with the
  healthy case: a one-frame drain latency is the normal majority state, so the inequality
  distinguished nothing. Number and name reserved.
- **Join rule corrected.** The 0.6.12 rule described a fallback path — an entry the
  producer could not bind to its frame was stamped with the *drain* frame's PTS. That
  structurally manufactured duplicate timestamps on a measurable fraction of frames, because the
  substitute PTS is also some other entry's correct one. The producer now **drops** an
  entry it cannot bind, so every entry on the wire carries its own frame's PTS and
  `timestamp` is a safe equality join. A dropped or lost entry shows as an `isp_frame_id`
  gap, which consumers should interpolate across rather than join on.

**Safe to break:** no consumer joins on the removed fields — `CameraFrameInfo` is
referenced only by the firmware that produces it. (Recordings carrying the stream do
exist, from boards with the frame-info stream enabled; they simply carry two fields nothing
reads, and a 0.7.0 consumer ignores them.) Ships with the matching firmware
producer change, which is what makes `timestamp` unique by construction.

## 0.6.12 — 2026-07-28

### Added `Command.set_recording_destination` (tag 36)

- Selects device/SD or phone-only recording behavior. Phone mode is leased so
  disconnecting the controlling app automatically restores device recording.

### Added `visio_schema.v1.sensor.CameraFrameInfo` — per-frame exposure + sensor timing (wire-compatible)

- **New payload type** on `/<device>/camera/<idx>/frame_info`, a sibling of the
  `CompressedVideo` topic: exposure actually in effect for the frame
  (integration time, analog/digital/ISP gains, ISO, integration lines) plus the
  sensor timing that makes any clip self-contained for rolling-shutter work
  (HTS / VTS / pixel clock — line time = `line_length_pixels /
  (pixel_clock_mhz * 1e6)`).
- **Deliberately a sibling topic, not a field on `foxglove.CompressedVideo`.**
  That schema is adopted as-is from the pinned foxglove-sdk submodule
  ([foxglove_compat.md](docs/protocol/foxglove_compat.md)); a same-name superset
  would collide in any consumer's descriptor pool that also loads the official
  definition. A sibling is also independently filterable via `SetStreamPolicy`.
- **Join rule**: `timestamp` is byte-identical to the described frame's video
  message — a plain equality join. The producer drains its ISP stats queue every
  frame and binds each entry to its frame by exact counter match against recent
  capture history (self-validating — no assumed offset); an unmatched entry
  (unknown-SDK safety net) is stamped with the drain frame's PTS and flagged by
  `vi_time_ref != isp_frame_id`. `vi_time_ref - isp_frame_id` doubles as the
  drain latency in frames.
  **⚠️ Superseded by 0.7.0** — real recordings showed the fallback produces
  duplicate timestamps and the flag is aliased with the healthy case. See above.
- New message on a new topic: wire-compatible in both directions. Old consumers
  ignore the unknown channel; the announce self-describes it for new ones.

## 0.6.11 — 2026-07-27

### Added `Heartbeat.client_id` (tag 5) — stable sender identity (wire-compatible)

- **New optional field on the beacon**: a per-install identity of the SENDER,
  constant across reconnects, transports and app restarts. `""` = not reported,
  so every existing client keeps working unchanged.
- **Why**: a device that allows one client at a time cannot otherwise tell "my
  phone is back" from "a second phone wants the board". A link dropped without a
  FIN — Wi-Fi provisioning (AP→STA), reset-to-hotspot, an app restart — leaves a
  socket its owner has already abandoned, and refusing the owner's next
  connection locks them out of their own device ("Device in use") until
  TCP_USER_TIMEOUT reaps the corpse, or indefinitely while the owner still holds
  both networks.
- **On the beacon** because it is the one message every healthy client already
  sends at 1 Hz, so identity costs no extra round trip. It therefore arrives
  shortly *after* accept; consumers resolve occupancy when it lands rather than
  at accept time.

## 0.6.10 — 2026-07-27

### Added `Command.clear_camera_tuning` (tag 35) — erase a unit's per-unit camera tuning (wire-compatible)

- **New no-field Command body `ClearCameraTuning`**, the counterpart to
  `SetCalibration{camera_tuning}`. A new per-unit white-balance correction cannot be
  measured on top of a live one (the measurement reads the residual *after* the
  correction), so the device now **refuses** a `camera_tuning` write while a correction
  is applied — `CommandResult{ok=false, error_code="correction_active"}`. Clearing erases
  the stored record and the device restarts so the next boot comes up uncorrected — the
  clean baseline a re-measure needs.
- **Deliberately its own command, not an empty `SetCalibration`.** The `camera_tuning`
  artifact requires ≥1 point and its write path is exactly what the gate blocks, so
  "clear" has to be a separate, ungated verb. Empty body — tuning is one record per unit.
- Works on **sealed units** (bus only, no adb), like the rest of the calibration path.

## 0.6.9 — 2026-07-26

### Added `Command.set_status_report` (tag 34), `SetStorage/TestStorage.status_prefix` (tag 7), `DeviceState.status_report` (tag 31) + `storage_status_prefix` (tag 32) — periodic device status reports

Devices can now periodically PUT a health snapshot (plus a low-res camera JPEG) to
the customer's S3/OSS bucket, so a fleet owner can see which units are alive,
aimed, hot, or filling their card without touching them.

- **`SetStorage.status_prefix` / `TestStorage.status_prefix`**: a SECOND key prefix
  over the SAME credentials. The recordings leg (`prefix`) and the status leg are
  independent — a device may do recordings only, status only, or both. Empty means
  the device default, `status/`. Deliberately a sibling of the recordings subtree,
  not nested inside it: the two want different lifecycle rules and different read
  grants, and health JSON must not appear where a recordings-ingest pipeline walks.
- **`Command.set_status_report`**: `SetStatusReport { enabled }`, the runtime
  on/off switch, persisted device-side. Separate from `SetAutoUpload`, which gates
  the recordings leg; the two share credentials and nothing else.
- **`DeviceState.status_report`**: **tri-state**, not a bool, for the same reason
  as `audio_recording` — `UNSUPPORTED` covers both pre-status-report firmware and
  status reporting disabled in the board config, letting the app hide a control it
  cannot move rather than render a switch in the wrong position.
- **`DeviceState.storage_status_prefix`**: echoed so the app can show where health
  reports land.

Wire-compatible in both directions: all four are new optional fields on new tag
numbers. Old apps ignore them; new apps see `UNSUPPORTED` / empty from old
firmware, which is exactly the "feature absent" reading.

NOTE on tags: an earlier draft of this feature targeted `Command` tag 33 and
`DeviceState` tag 30. Both were taken by `forget_wifi` / `wifi_networks` in 0.6.8
while this was in progress — re-derived to 34 and 31/32. Always re-check against
`origin/main` before claiming a tag; see the 0.4.x note about `set_notice_lang`
having to move 26 → 27 for the same reason.

## 0.6.8 — 2026-07-26

### Added `Command.forget_wifi` (tag 33) + `DeviceState.wifi_networks` (tag 30) — many remembered networks

- **`DeviceState.wifi_networks`**: `repeated WifiNetwork { ssid }`, **newest first** — the
  ordered set the device walks whenever it is offline. Index 0 is the most recently
  joined. Previously a device remembered exactly one network and every join erased the
  previous one, so a rig carried between a factory floor, a lab and a hotspot had to be
  reprovisioned on every move.
- **`ConnectWifi` (tag 14) is unchanged on the wire but gains a documented side effect**:
  a successful join puts its SSID at the FRONT of the list, moving it (and replacing the
  stored passphrase) if it was already there. There is deliberately **no reorder command** —
  joining *is* the reorder, so there is no second way for host and device to disagree
  about the order.
- **`ForgetWifi { ssid }`**: removes ONE entry. A **list edit, not a radio action** — even
  when it names the network the device is currently on, the association survives, so the
  caller's link survives and the ack actually gets back. `ResetToAp` remains the "leave
  now" verb and is now documented as forgetting **all** of them. Idempotent: an unknown
  ssid answers `ok=true`, so two clients rendering the same polled list cannot race into a
  spurious failure.
- **No passphrase is ever carried outbound.** `WifiNetwork` has no field for one and never
  will — the rule `storage_access_key_id` already follows for the S3 secret.
- **No `connected` flag** on an entry: compare its ssid against `wifi_ssid` (meaningful
  only while `wifi_state == WIFI_STATE_STA`). One source of truth, so nothing can go stale
  against the actual connection.
- **Reading an empty list**: proto3 gives `repeated` no presence, so "old firmware", "this
  device manages no networks" and "the device is on a network it cannot manage" (a
  WPA-Enterprise or hidden block in its stored config) are indistinguishable — and all
  three mean the same thing to a client. An empty list next to a **non-empty `wifi_ssid`**
  is *"this network exists but I cannot manage it"*, never *"nothing is saved"*: render the
  one network you know about and hide the per-entry forget. Do not probe with `ForgetWifi`;
  its `unsupported` is a permanent refusal.
- **nanopb**: `ForgetWifi.ssid` is `max_size:33`, matching `ConnectWifi.ssid` so a 32-octet
  SSID cannot be accepted by one and truncated by the other. `wifi_networks` is
  `FT_POINTER` — a fixed array would add bytes to *every* `CommandResult` ack forever to
  carry a list that is usually one entry, and the malloc-free rule governs the inbound
  `Command` decode path, which this does not touch.
- The device caps what it stores (a runaway backstop, not a wire limit — nothing decodes
  into a fixed array), so unlike `SetStreamPolicy.rules` the bound is **not** part of this
  contract.

## 0.6.7 — 2026-07-25

### Added `Command.set_stream_policy` (tag 32) — the generic per-connection stream filter

- **New `SetStreamPolicy { repeated Rule { topic, drop, max_rate_hz } }`**: one
  ordered, first-match-wins list saying which streams a connection wants and how
  fast, addressed by **topic glob** rather than by message class. It replaces the
  connection's policy outright (no merging), and a connection that never sends one
  gets everything at full rate — so a lost command still costs bandwidth, never
  data, and a client recording from the live stream stays lossless by not asking.
  `target_device` is ignored: a policy describes one link, so it applies at the hop
  it arrives on (on a suit, the hub→phone leg that actually binds).
- **Why**: the two knobs it replaces keyed off a *message flag* (`bulk`,
  `decimatable`), never the topic, so "camera 0 yes, camera 1 no" was inexpressible
  and the raw IMU bundles — which carry neither flag — could not be shed at all. A
  phone preview renders one camera at ~15 Hz but was pulling both H.265 eyes plus
  the full IMU set.
- **Glob grammar**: leading `/` stripped from both sides, then `*` matches exactly
  one path segment and `**` matches zero or more segments at any position.
  Everything else is a literal segment; there are no partial-segment globs. Patterns
  are re-resolved whenever a peer learns a channel, so a rule covers leaves a hub has
  not discovered yet. Prefer a **leading** `**` when the depth is not yours to know:
  relayed leaf topics normally arrive unchanged, but a relay MAY namespace them under
  the leaf's `device_name` (an opt-in relay setting, off by default, opted
  into for multi-device bring-up). `**/camera/0` matches both forms.
- **`max_rate_hz` is ignored for camera video.** H.265 is inter-coded: shedding
  arbitrary P-frames costs the decoder its reference chain and blanks the viewer for a
  whole GOP. Video is keep-or-drop; capping is for streams whose messages stand alone.
- At most **12 rules** — the bound the device's malloc-free decode struct is sized
  for. A longer list fails to decode as a whole and carries no `command_id` to report
  against, so it is part of the contract.

### `CommandResult`: an unrecognized DIRECTED command must be refused, not ignored

- A receiver now MUST answer a Command naming it in `target_device` even when it
  does not implement it — `ok=false, error_code="unsupported"` — and callers must
  treat that code as PERMANENT (no retry). Broadcasts (empty `target_device`) stay
  silent, since they reach every leaf behind a hub and N replies to one
  `command_id` is worse than none.
- **Why**: silence is indistinguishable from a slow link, so a newer host spends
  its full command timeout *and* its retry budget on something that can never
  succeed — against firmware predating tag 32 the app's stream-policy gate did
  exactly that, then silently gave up, losing the non-video-screen pause and the
  join-keyframe with only a console warning. This makes a version mismatch fail
  fast and legibly instead of on a stopwatch.
- Wire-compatible: no field or tag changes, only a contract the `error_code`
  string already had room for.

### Deprecated `set_video_streaming` (23) and `set_imu_live_rate` (31)

- Both are marked `deprecated` and are now **sugar over the policy**, not a second
  mechanism: a receiver expands `SetVideoStreaming{false}` into "drop every
  `foxglove.CompressedVideo` channel" and `SetImuLiveRate{hz}` into "cap every
  `Quaternion` channel", using the schema names the registry already carries. Fielded
  app builds are unaffected; old and new clients share one enforcement path.
- Transport (internal, unpinned): `Endpoint::SetBulkPaused` / `SetLiveRateHz` are
  **removed** in favour of `SetStreamPolicy(shared_ptr<const ResolvedStreamPolicy>)`,
  and `FramedFdEndpoint::Send` now has a single filter gate instead of two. The filter
  still runs before `EncodeFramed`, so a shed frame costs no CPU. `RequestBulkFlush()`
  absorbs the old pause-shed: it is called on the policy edges where video stops (shed
  the backlog) or resumes (drop pre-join video ahead of the keyframe).
- **Recording sinks are untouched by any of this.** `SetStreamPolicy` is a no-op on
  the `Endpoint` base and only `FramedFdEndpoint` overrides it, so a viewer thinning
  its own preview cannot thin an MCAP — the reason the filter is per-endpoint.

## 0.6.6 — 2026-07-24

### Added `Command.set_imu_live_rate` (tag 31) — per-connection IMU live-rate cap

- **New `SetImuLiveRate { uint32 rate_hz }`** on the Command oneof, scoped to
  the sending connection exactly like `SetVideoStreaming`. Caps the live
  delivery rate of per-sample derived IMU streams (the fused quaternions) to
  that endpoint only. `0` — and a fresh connection — mean full rate, so a
  client that records from the live stream and never asks stays lossless.
  Raw IMU bundles and on-device recordings are never affected. Wire-compatible
  MINOR addition; an old device ignores the unknown body, an old app simply
  never sends it.
- Transport (internal, unpinned): framed sinks now encode each outbound
  message once and fan the shared framed buffer out by refcount; the control
  outbox batches its drain (one write per pass instead of one per ~470 Hz IMU
  message); a sink whose link accepts nothing for 3 s with bytes pending
  stops paying to frame bulk/decimatable traffic until the reader returns;
  accepted TCP connections carry `SO_KEEPALIVE` + `TCP_USER_TIMEOUT` (~10 s)
  so a vanished peer frees its endpoint promptly.

## 0.6.5 — 2026-07-24

Tooling and C++ library only — **no proto or wire change** (`make breaking` clean
against `main`). Cut as the first tag since `v0.4.2`, so it also ships the
accumulated `0.5.0`–`0.6.4` (see those entries), including the **breaking `0.6.0`
`ImuCalibration` slim**.

### `McapWriterEndpoint` survives a storage failure instead of terminating (C++)

- The writer thread had no exception handling. `McapWriter` throws when it cannot
  open the next part, so a card that fills up — or that the kernel flips read-only
  after an integrity error — threw out of the thread's entry function, which is
  `std::terminate`. A card that goes read-only mid-recording therefore took down
  the whole process on the next part rotation rather than failing the recording
  alone.
- The failure is now caught and **latched** behind a new `write_failed(reason)`
  query; the thread stays alive to shed what is queued so `Send()`/`Stop()` stay
  well-behaved, and the owner polls the latch and stops the recording. `Stop()`
  guards `Close()` the same way — it runs from a `noexcept` destructor, and losing
  a footer costs one part's index, which the uploader's torn-part repair recovers.
- Not routed through `on_closed`: that contract means "fixed link hit EOF, detach
  me", a different thing from "storage died, stop recording", and a write-only
  sink ignores both callbacks.

### `visio-display --serve`: load & replay a local MCAP file (launcher)

- New "Recording / MCAP file" source replays a local recording into the same
  Foxglove WebSocket bridge, reusing the existing H.265→JPEG decode fallback so
  replayed video transcodes unchanged for browsers that can't decode HEVC. The
  file is opened by path (nothing is uploaded); a server-side file browser
  (`GET /api/fs`, `POST /api/mcap`) picks it, since a browser can't hand the
  server a filesystem path.
- Also fixes a latent launcher bug: `_run` passed a zero-arg `on_closed`, but the
  fd and MCAP-reader endpoints call `on_closed(self)`, so a replay EOF (or a live
  TCP unplug) would `TypeError`; a terminal "ended" state now replaces the stale
  "streaming" a finished source reported.

## 0.6.4 — 2026-07-23

### Added `SetCalibration.camera_tuning` — per-unit camera measurement (wire-compatible)

- **New `visio_schema.v1.calibration.CameraTuning` (`WbMeasurement` + `WbPoint`)**,
  carried on the `SetCalibration` artifact oneof at **tag 15**, `sensor_kind =
  CAMERA`. Optics vary unit to unit, so one per-model tuning cannot be
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
  `rg` *is* the residual error, and a chosen red gain of G is simply the point
  `rg = 1/G`. A later fixture measurement then replaces it at the same CCT
  without the record changing shape. At least one point is required.
- **Carries measurements and nothing else.** The model that extends a
  measurement across colour temperature and the resulting pipeline values live on
  the device, so improving either is an OTA rather than a re-push of every unit.
- **Indexed by CCT**, not by illuminant name or a pipeline-side light-source slot,
  so changing that light-source list does not reinterpret stored records.
  `mired` is absent (derivable as `1e6/cct`; carrying both invites a record whose
  two indices disagree).
- **One record per unit, not per camera.** `sensor_index` is still required by
  `SetCalibration` but selects nothing here: the correction applies to the unit as
  a whole, so a per-camera artifact would have promised a granularity the hardware
  does not offer.
- **`lens_model` / `lens_batch` are the only identity fields on the wire**,
  because the lens is the one thing no device can sense. `lens_model` is required;
  `lens_batch` is recorded and logged but **never gates**, since correcting a unit
  from a new lens batch is the entire purpose.
- **Deliberately NOT on the wire**: the sensor and the tuning revision. A
  host tool can observe neither, so a value it sent would be an assertion about
  state it cannot see. The device stamps its own when it stores a record and
  re-checks at apply, catching a reflash between calibration and use.
- Field numbers 2-4 reserved on `CameraTuning` for lens-shading, black-level and
  defect-pixel artifacts.

## 0.6.3 — 2026-07-21

### `SetAudioRecording` — turn mic capture off without a reboot (wire-compatible)

- **New `SetAudioRecording` command (tag 30).** Enables/disables microphone
  capture reaching recordings, persisted device-side and applied live. Both the
  device's own MCAP and the phone-side MCAP record what reaches the bus, so the
  one switch covers both; a recording made while disabled has no audio channel
  at all rather than an empty one. Only mic-equipped boards accept it — every
  other device rejects it, as with `SetNoticeLang`. Default enabled, so existing
  units are unchanged by an upgrade.
- **New `DeviceState.audio_recording` (tag 29), a tri-state enum.**
  `UNSUPPORTED` (0) covers both "no mic on this board" and "firmware predating
  the toggle", so a host can hide the control rather than guess. Deliberately
  not a bool: proto3 would hand an old device's `false` to the host, which would
  then show the switch OFF while that device was in fact recording audio.
- Address `SetAudioRecording` to a specific device. An empty `target_device` is
  a broadcast, which on a suit would flip every mic-equipped board at once.

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
  — the device tears that link down to switch radios (it cannot hold an AP and an STA association at once),
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
  read the board's real time (the boards boot to 1970 until SetTime).

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
