# Changelog

All notable wire-contract changes to `visio-schema`. Versioning follows
[`docs/protocol/versioning.md`](docs/protocol/versioning.md). Pre-1.0, breaking changes
bump the MINOR version.

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
