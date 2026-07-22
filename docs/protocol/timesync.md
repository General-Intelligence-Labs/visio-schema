# Visio Timesync — heartbeat beacon + offset filter

Visio aligns peer clocks with an NTP-style exchange that rides the
**heartbeat beacon** — there is no separate timesync stream. Every peer
periodically beacons a `Heartbeat` on the hop-local
`CONTROL_STREAM_HEARTBEAT` control stream; the beacon doubles as a
liveness ping and one leg of a clock-offset exchange. Each peer maintains a
per-neighbour offset and applies it to every inbound message's
`Header.timestamp`, shifting it into the receiver's own clock domain.

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

This document is canonical. Implementations MUST conform. For the client-side
recipe — running the exchange and applying the offset, with a runnable example
— see [`../timesync_client.md`](../timesync_client.md).

> **History.** Earlier Visio used a dedicated `STREAM_TIMESYNC` with
> `Request`/`Response` messages. That stream is gone; the exchange was
> folded into the heartbeat beacon.

## 1. Addressing — keyed by endpoint, not device

The wire `Header` carries only `{stream_id, seq, timestamp}` — there is no
device field. Heartbeat rides a **control stream** (`stream_id <
CONTROL_STREAM_FIRST_DYNAMIC`), which a receiver handles **hop-locally**
and never relays. So a "peer" is simply the endpoint a beacon arrives on:
all per-neighbour state (last-seen, the offset window) MUST be keyed by
that connection. Links are point-to-point, so one endpoint == one neighbour.

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
offset = (echo_tx_mono_ns + rtt / 2) - echo_rx_mono_ns
```

- `rtt` is the full round trip (the responder replies immediately, so its
  processing time is folded into the RTT — kept small by the immediate
  reply).
- `offset` is **ours minus the responder's** clock at the RTT **midpoint** —
  the classic two-timestamp NTP estimate assuming a symmetric path. `Add` it
  to a timestamp from that peer to convert it into our clock (§4): a peer
  timestamp `T` maps to `T + offset = T + (ours - peer) = ours`. (The reverse
  sign, `peer - ours`, would move the rewrite the wrong direction.)

### Sliding-window outlier filter

A single estimate is noisy (scheduling, USB/I2C jitter). Each neighbour's
clock estimator keeps a sliding window (default 8) of `(rtt, offset)` samples
and uses the **lowest-RTT** sample's offset — the low-RTT sample has the
least queuing jitter, so its midpoint estimate is the most accurate.
Samples with `rtt <= 0` or `rtt > 100 ms` are discarded as outliers.

This min-RTT window is the **minimum** conforming filter. An implementation MAY
do better on the same samples — e.g. a drift-compensated windowed linear
regression over the low-RTT samples (fit `offset = a + b·t`, drop the worst
residual, refit, evaluate at now), which additionally tracks crystal drift
between beacons. The wire exchange is identical either way.

## 4. Receive-side rewrite contract

The rewrite runs on **every** inbound message, before per-stream handling
and before any relay. It shifts `Header.timestamp` into the receiver's clock
using the converged offset for the connection the message arrived on:

```python
def rewrite_timestamp(msg, from_ep):
    offset_ns = peer_offset(from_ep)
    if offset_ns is None:
        return                      # not converged → leave the timestamp as-is
    msg.timestamp.FromNanoseconds(msg.timestamp.ToNanoseconds() + offset_ns)
```

The beacon's own T-values live in the **payload** (bare `uint64`), so this
Header rewrite never corrupts them. Convergence MUST be **queryable** by
consumers — "no offset yet" is a distinct state from an offset of zero, and
a consumer that needs a single timeline gates on it.

## 5. Wire-close stamping

Sync accuracy depends on `tx_mono_ns` being stamped as close to the
physical write as possible — queueing delay between stamping and the wire is
one-way, so it biases the RTT and hence the offset by half of itself. A
beacon MUST therefore bypass any send queue an implementation keeps for
data, and stamp both `Header.timestamp` and the payload `tx_mono_ns` at the
moment of the write.

## 6. Roles

Every peer beacons, so every peer is **both** initiator and responder —
sync is symmetric. An embedded device need do nothing special: it beacons
like everyone else and answers initiating beacons it receives.

## 7. Convergence target

On a healthy USB CDC link with ~1 ms RTT the filter converges within a
handful of beacons. The immediate-reply, midpoint estimate is sufficient
for cross-peer sensor alignment; it is not intended for sub-µs latency
analysis. These bounds are the conformance target an implementation's
convergence test should assert.
