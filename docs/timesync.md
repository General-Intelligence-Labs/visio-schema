# Visio Timesync — NTP-style two-way exchange + offset filter

Visio uses an NTP-style four-timestamp exchange to align peer clocks.
The exchange itself rides the regular bus on `STREAM_TIMESYNC`; every
peer's local `TimesyncService` maintains a per-sender offset table
that the Bus consults on every inbound message to rewrite the wire
`Header.timestamp` into the receiver's own clock domain.

This document is canonical. Implementations MUST conform.

## 1. The four timestamps

Each exchange consists of one `Request` and one `Response`:

```
Initiator                                           Responder
─────────                                           ─────────
t0 = local_mono_now()
  │ Request{ t0, initiator, exchange_seq }
  ▼
  ──────────────────────────────►          t1 = local_mono_now()
                                               (stamped on rx)

                                            … responder work …

                                           t2 = local_mono_now()
                                                (stamped just before tx)
                                            Response{ t0, t1, t2,
                                                      initiator,
                                                      responder,
                                                      exchange_seq }
  ◄──────────────────────────────
t3 = local_mono_now()
   (stamped on rx — locally,
    not carried on the wire)
```

All four timestamps are in **nanoseconds**, in the **producer's local
monotonic clock**. No Unix-epoch conversion happens in the timesync
math — the offsets are between mono clocks, and Unix conversion is
deferred to MCAP-write time (separate concern, see
[`framing.md`](framing.md) section 3.5).

### Wire shape

```proto
package visio.service.timesync.v1;

message Request {
  uint64 t0           = 1;
  DeviceClass initiator = 2;
  uint64 exchange_seq = 3;
}

message Response {
  uint64 t0           = 1;   // echoed
  uint64 t1           = 2;   // responder rx
  uint64 t2           = 3;   // responder tx
  DeviceClass initiator = 4;
  DeviceClass responder = 5;
  uint64 exchange_seq = 6;   // echoed
}
```

(`DeviceClass` and timestamps are bare `uint64` here, not
`google.protobuf.Timestamp` — see "Why uint64 in timesync only"
section below.)

## 2. Derivation

The initiator computes on each successful Response:

```
rtt    = (t3 - t0) - (t2 - t1)
offset = ((t1 - t0) + (t2 - t3)) / 2
```

- `rtt` is the round-trip time, excluding the responder's processing
  time between t1 and t2.
- `offset` is the value to ADD to the responder's clock to convert
  it into the initiator's clock. I.e., if a responder timestamp is
  `T_r`, the initiator-clock equivalent is `T_r + offset`.

The sign convention matters: a positive `offset` means the
responder's clock is **behind** the initiator's.

## 3. Sliding-window outlier filter

A single exchange's offset is noisy (variance from kernel scheduling,
I2C / USB transfer jitter, etc.). Each peer's `TimesyncService`
maintains a **sliding window of the last N exchanges per peer** and
uses the lowest-RTT subset as the offset estimate.

Recommended parameters:

| Parameter | Value | Why |
|---|---|---|
| Window size `N` | 8 | Enough samples to detect outliers; small enough to track real clock drift. |
| Selected subset | 4 lowest-RTT samples out of N | Low-RTT samples have less queuing jitter, so their offsets are more accurate. |
| Offset estimate | median of the 4 selected `offset` values | Robust to remaining outliers without overweighting the minimum. |
| Exchange cadence | once per second per peer pair | Standard NTP-ish cadence; tighter on initial sync (5×/s for the first 10 s after a peer is discovered). |

The 8/4/median triple is the well-known NTP "minimum-filter +
clock-select" heuristic, sized for our small-fleet topology rather
than the public internet.

### Pseudocode

```python
class PeerOffset:
    window: deque[(rtt, offset)] = deque(maxlen=8)

    def push(self, rtt, offset):
        self.window.append((rtt, offset))

    def estimate(self) -> int | None:
        if len(self.window) < 4:
            return None  # not enough data yet
        sorted_by_rtt = sorted(self.window, key=lambda p: p[0])
        best4 = sorted_by_rtt[:4]
        offsets = sorted(o for _, o in best4)
        return offsets[len(offsets) // 2]   # median
```

## 4. Bus rx-rewrite contract

When the Bus dispatches an inbound `Message` to subscribers, it
rewrites the Header `timestamp` field IN PLACE before fanout and
attaches a per-message `timestamp_synced` flag visible to subscribers:

```python
def on_inbound(self, msg: Message):
    sender = msg.header.routed_from
    offset = self.timesync.peer_offset(sender)  # None until converged
    if offset is not None:
        msg.header.timestamp = add_ns(msg.header.timestamp, offset)
        msg.timestamp_synced = True
    else:
        # Pre-convergence: forward the message but mark the timestamp
        # as untrustworthy. The Header field is left at the producer's
        # clock value (we do NOT zero or sentinel it — replay must still
        # reconstruct).
        msg.timestamp_synced = False
    self._dispatch(msg)
```

**Subscriber contract**: subscribers MUST check `msg.timestamp_synced`
before using `msg.header.timestamp` for any cross-peer alignment or
correlation. Implementations of the in-memory `Message` struct
(`visio-mq/cpp/`, `visio-mq/python/`) MUST expose this flag.

The flag is NOT serialized on the wire — it is a property of the
local Bus's knowledge state, not of the message itself. A peer that
reads the same MCAP later may have full timesync data and would set
the flag to True; another peer reading the same file mid-startup
would set it False. The flag is a runtime assertion, not durable
metadata.

Recommended subscriber-side behaviors when `timestamp_synced == False`:

- **High-rate sensor producers** (publishers): unaffected — they
  publish using their own local clock, no read of `Header.timestamp`
  needed.
- **Sensor consumers that align across peers** (e.g., LeRobot
  exporter): drop or buffer the message until later cycles where
  `timestamp_synced == True`. The brief startup window is acceptable
  loss for the alignment guarantee.
- **MCAP recorder**: write the message; rely on MCAP's own log_time
  (which it computes from `Header.timestamp` + the recorder's
  mono→Unix offset). The recorder MAY annotate the MCAP record's
  `metadata` map with `"timestamp_synced": "false"` so downstream
  replay can filter; this is implementation-defined.
- **Service responders** (timesync, deviceinfo): unaffected — they
  use receive-side wall-clock or fresh local timestamps.

Important subtleties:

- The offset is **keyed by `routed_from`** (the immediate previous
  hop), NOT `device` (origin). On a relay chain `A → B → C`, the
  receiver C uses the offset for B (because B already rewrote
  `timestamp` on its own rx).
- The Bus does NOT rewrite payload-internal timestamps (e.g., the
  `Imu.timestamp` inside the payload bytes). Those stay in the
  origin producer's clock domain. Consumers that want consistency
  use Header.timestamp; consumers that need the raw producer time
  use the payload field.
- A subscriber's handler receives a `Message` whose `header.timestamp`
  is "in this receiver's local mono clock" — always — once timesync
  has converged. Subscribers don't do offset math themselves.

## 5. Initiator / responder roles

Any peer can be either an initiator or a responder, or both. By
convention:

- **Hosts and recorders** initiate exchanges with every connected
  device peer at the standard cadence.
- **Embedded devices** (grippers, gloves) act as responders. They do
  not initiate (no need — they only care about correlating their own
  data with the host).
- **The Quest** is a responder for the gripper / host's queries; it
  also initiates with the backend it uploads to.

A peer that wants to "go offline cleanly" should drop new Request
arrivals after announcing in DeviceInfo that it's shutting down;
silent drops on existing exchanges are fine.

## 6. Why uint64 in timesync only

Every other Visio schema uses `google.protobuf.Timestamp` for time
fields (see [the master plan](../MASTER_PLAN.md#4-core-decisions-locked-from-design-conversation)).
Timesync is the lone exception — `t0/t1/t2` are bare `uint64`.

Reason: timesync math operates on raw integer nanosecond differences
in the producer's local clock. `google.protobuf.Timestamp` splits
into `(seconds, nanos)` which the math would have to re-merge on
every operation. The Timestamp shape exists for Unix-epoch
interpretation; for raw mono nanoseconds there's nothing to
interpret. Bare `uint64` is the right tool here.

This is documented inside `timesync.proto` so the inconsistency is
visible at the schema level.

## 7. Failure handling

| Condition | Behavior |
|---|---|
| Responder doesn't reply within (5 × current RTT) or 100 ms, whichever is greater | Initiator drops the in-flight `exchange_seq`, retries on next cadence tick. |
| RTT > 10 ms on a USB CDC link, > 100 ms on Wi-Fi | Sample still recorded; the lowest-RTT-4 filter naturally rejects it if it's an outlier. |
| Offset estimate hasn't been computed yet (< 4 samples) | Bus does NOT rewrite `Header.timestamp`; sets `Message.timestamp_synced = False` so subscribers can detect and either drop or buffer. Log at info-level on the FIRST unsynced message from each peer (not per-message — too noisy at startup). |
| Wall-clock catastrophic offset (> 1 s drift between consecutive estimates) | Almost certainly a peer rebooted. Reset the per-peer window and re-sync. Don't propagate the discontinuity into subscribers. |

## 8. Convergence target

On a healthy USB CDC link with ~1 ms RTT, the filter converges to
within ±50 µs of the true offset in ~4-5 exchanges (i.e., 4-5
seconds at the 1 Hz steady-state cadence; sub-second during the
initial fast-sync window).

On Wi-Fi the convergence is to ±500 µs, sufficient for sensor
alignment but not for fine-grained latency analysis.

These bounds are the conformance target for
`visio-mq/tests/interop/test_timesync.py`.
