# Visio Timesync — heartbeat beacon + offset filter

Visio aligns peer clocks with an NTP-style exchange that rides the
**heartbeat beacon** — there is no separate timesync stream. Every peer
periodically beacons a `Heartbeat` on the hop-local
`CONTROL_STREAM_HEARTBEAT` control stream; the beacon doubles as a
liveness ping and one leg of a clock-offset exchange. Each peer's local
`HeartbeatService` maintains a per-neighbour offset that the Bus applies
to every inbound message's `Header.timestamp`, shifting it into the
receiver's own clock domain.

**Producer contract.** A data stream's `Header.timestamp` MUST be the
payload's **sensor capture time** — the instant the measurement was taken,
in the producer's local monotonic clock, identical to the time the payload
self-describes with (`CompressedVideo.timestamp`, `ImuRaw.first_sample_time`,
…). It MUST NOT be the publish/encode/send instant: that latency jitters
per-frame and per-stream, and would corrupt both intra-stream rate and
inter-stream (stereo, cam↔IMU) alignment. Only control/transport messages
with no sensor instant (heartbeat, DeviceInfo, command results) stamp the
send time. With this contract the rx-rewrite (§4) turns a data header into
"capture time in the receiver's clock" — directly fusable across devices,
while the payload retains the producer-clock original.

This document is canonical. Implementations MUST conform.

> **History.** Earlier Visio used a dedicated `STREAM_TIMESYNC` with
> `Request`/`Response` messages. That stream is gone; the exchange was
> folded into the heartbeat beacon.

## 1. Addressing — keyed by endpoint, not device

The wire `Header` carries only `{stream_id, seq, timestamp}` — there is no
device field. Heartbeat rides a **control stream** (`stream_id <
CONTROL_STREAM_FIRST_DYNAMIC`), which the Bus dispatches **hop-locally**
and never relays. So a "peer" is simply the endpoint a beacon arrives on:
all per-neighbour state (`last_seen`, the offset window) is keyed by
`id(from_ep)`. Links are point-to-point, so one endpoint == one neighbour.

## 2. The exchange

```
Initiator                                          Responder
─────────                                          ─────────
beacon: tx_mono_ns = A1   ───────────────────►   rx at B (responder's clock)
  (A's clock, stamped at wire)                   responder REPLIES immediately:
                                                   echo_tx_mono_ns = A1   (echo)
                                                   echo_rx_mono_ns = B    (its rx)
                                                   tx_mono_ns      = B2   (its own send)
rx at A4 = local_mono_now()  ◄───────────────────
A matches echo_tx_mono_ns == A1 (a send it made)
and closes the loop.
```

A beacon is an **initiating** beacon if `echo_tx_mono_ns == 0`, and a
**response** beacon otherwise. A responder answers every initiating beacon
immediately with a response; it does not answer response beacons (so there
is no ping-pong). Both directions beacon, so sync is symmetric.

### Wire shape

```proto
package visio_schema.v1.service.heartbeat;

message Heartbeat {
  uint64 tx_mono_ns      = 1;   // sender send time, stamped at the wire
  uint64 echo_tx_mono_ns = 2;   // echoed peer tx (0 ⇒ initiating beacon)
  uint64 echo_rx_mono_ns = 3;   // responder's rx of that peer beacon
  uint32 queue_depth     = 4;   // optional backpressure hint
}
```

All timestamps are **nanoseconds** in the **producer's local monotonic
clock**, bare `uint64` (not `google.protobuf.Timestamp`) so the receiver
reads the raw mono clock, immune to the `Header.timestamp` rewrite
(section 4). Unix-epoch conversion is deferred to MCAP-write time.

## 3. Derivation

On receiving a response beacon whose `echo_tx_mono_ns` matches one of our
own recent sends, the initiator computes (all in **its own** clock except
`echo_rx_mono_ns`, which is the responder's):

```
rtt    = now - echo_tx_mono_ns
offset = echo_rx_mono_ns - (echo_tx_mono_ns + rtt / 2)
```

- `rtt` is the full round trip (the responder replies immediately, so its
  processing time is folded into the RTT — kept small by the immediate
  reply).
- `offset` is the responder's clock minus ours at the RTT **midpoint** —
  the classic two-timestamp NTP estimate assuming a symmetric path. Add it
  to a timestamp from that peer to convert it into our clock.

### Sliding-window outlier filter

A single estimate is noisy (scheduling, USB/I2C jitter). Each neighbour's
`_PeerClock` keeps a sliding window (default 8) of `(rtt, offset)` samples
and uses the **lowest-RTT** sample's offset — the low-RTT sample has the
least queuing jitter, so its midpoint estimate is the most accurate.
Samples with `rtt <= 0` or `rtt > 100 ms` are discarded as outliers.

## 4. Bus rx-rewrite contract

The `HeartbeatService` registers an **all-streams middleware** handler —
`bus.on_message(None, ...)` — that runs on every inbound before per-stream
handlers and before any relay. It shifts `Header.timestamp` into our clock
using the converged offset for the arriving endpoint:

```python
def _rewrite_timestamp(self, msg, from_ep):
    pc = self._peers.get(id(from_ep))
    if pc is None or pc.offset_ns == 0:
        return                      # not converged → leave as-is
    msg.timestamp.FromNanoseconds(msg.timestamp.ToNanoseconds() + pc.offset_ns)
```

The beacon's own T-values live in the **payload** (bare `uint64`), so this
Header rewrite never corrupts them. Convergence is **queryable**:
consumers call `services.heartbeat.offset_ns(from_ep)` (None until a
sample lands).

## 5. Wire-close stamping (the priority send)

Sync accuracy depends on `tx_mono_ns` being stamped as close to the
physical write as possible — queueing delay between stamping and the wire
biases the RTT. The beacon is therefore sent via the bus **priority path**
(`publish_priority`), which bypasses the outbox, stamps `Header.timestamp`
and the payload `tx_mono_ns` at the wire moment via a `finalize` callback,
then fans out. The send runs on the bus loop thread (it is a
`schedule_periodic` callback), so the direct write is race-free.

## 6. Roles

Every peer beacons, so every peer is **both** initiator and responder —
sync is symmetric. An embedded device need do nothing special: it beacons
like everyone else and answers initiating beacons it receives.

## 7. Convergence target

On a healthy USB CDC link with ~1 ms RTT the filter converges within a
handful of beacons. The immediate-reply, midpoint estimate is sufficient
for cross-peer sensor alignment; it is not intended for sub-µs latency
analysis. These bounds are the conformance target for the host
`tests/test_heartbeat.py` convergence test.
