# Changelog

All notable wire-contract changes to `visio-schema`. Versioning follows
[`docs/protocol/versioning.md`](docs/protocol/versioning.md). Pre-1.0, breaking changes
bump the MINOR version.

## 0.2.0.dev0 — unreleased

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
