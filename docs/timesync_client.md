# Synchronizing your clock with a device (heartbeat NTP)

Every message a Visio device sends you carries a `Header.timestamp`: the **capture time of that
payload, in the device's own monotonic clock**. That clock counts from the device's boot, not from
any shared epoch, so its timestamps and your machine's are not comparable — subtract one from the
other and you get the difference in uptimes, not in time.

Visio closes that gap with an **NTP-style round trip folded into the heartbeat beacon**. The device
already beacons at you and answers your beacons; run the other half of the exchange and you learn
the offset between the two clocks, which is all you need to put the device's samples on your
timeline.

This page is the how-to. The normative algorithm — wire shape, formulas, filter, the receive-side
rewrite — is [`protocol/timesync.md`](protocol/timesync.md). A complete, runnable client is
[`examples/python/timesync_client.py`](../examples/python/timesync_client.py):

```bash
python examples/python/timesync_client.py /dev/ttyACM0
```

## Do you need it?

| What you're doing | Timesync? |
|---|---|
| Comparing one device's streams to each other — stereo cameras, camera ↔ IMU, audio ↔ video | **No.** They're all stamped by the same clock, so they're already aligned. |
| Putting device data on **your** timeline — your wall clock, your own sensor logs, a robot controller, a second Visio device | **Yes.** This page. |
| Replaying an MCAP you recorded *after* applying the offset | **No.** It was done at record time; the file is already on one timeline. |

---

## 1. How the exchange works

Everything rides the `CONTROL_STREAM_HEARTBEAT` control stream (`stream_id` 3) — there is no
separate timesync stream. A beacon is a
`visio_schema.v1.service.heartbeat.Heartbeat`, and it comes in two flavours, distinguished by
whether `echo_tx_mono_ns` is set:

```
you                                                      the device
───                                                      ──────────
initiating beacon: tx_mono_ns = A1  ──────────────────►  received at B (device clock)
  (your clock, stamped at the wire)                      it replies IMMEDIATELY with
                                                           echo_tx_mono_ns = A1   (your send, echoed)
                                                           echo_rx_mono_ns = B    (its receive)
                                                           tx_mono_ns      = B2   (its own send)
received at A4 = your clock now      ◄──────────────────
you recognise A1 as a send of yours and close the loop:

    rtt    = A4 - A1                              both in YOUR clock
    offset = (A1 + rtt/2) - B                     your clock MINUS the device's
```

`echo_tx_mono_ns == 0` marks an **initiating** beacon; anything else is a **response**. Both sides
beacon and both sides answer, so the exchange is symmetric — you should implement both halves
([§3a](#a-respond--answer-the-devices-beacons) and [§3b](#b-initiate--beacon-on-a-timer)).

All three timestamps are bare `uint64` nanoseconds in the **sender's** local monotonic clock. They
are deliberately *not* `google.protobuf.Timestamp` and deliberately live in the payload, so they
survive any rewriting you do to `Header.timestamp`.

---

## 2. The one thing to get right: the sign

```
local_ns = peer_ns + offset_ns          where  offset_ns = (your clock) − (the device's clock)
```

**Add** the offset to a timestamp that came from the device. Flipping it is the classic bug and it
is silent: the timestamps stay perfectly plausible, they just land 2 × offset away — a recording
that looks fine until you notice the camera fires before the IMU sample that triggered it.

Two more properties worth internalising:

- **Rewrite the header; leave the payload alone.** Once you shift `Header.timestamp` it is in
  *your* clock, but the same instant inside the payload (`ImuRaw.first_sample_time`,
  `CompressedVideo.timestamp`, `RawAudio.timestamp`, and the base each `ImuSample.t_offset_ns` is
  measured from) is still in the **device's** clock. Keeping the payload untouched preserves the
  device's original record; convert those with the same `+ offset_ns` as you decode them.
- **One offset per link.** The offset describes the connection you measured it on, and heartbeat
  carries no device identity — it is answered by whoever is on the other end of that link. If you
  open two devices, run two independent exchanges and keep two offsets.

---

## 3. Implementing it — four pieces

All four are in [`examples/python/timesync_client.py`](../examples/python/timesync_client.py); the
snippets below are its core, with imports elided.

### a. Respond — answer the device's beacons

The device beacons at you on its own timer. A beacon with `echo_tx_mono_ns == 0` is **initiating**
and you must answer it immediately, echoing its send time and stamping your receive time. Reply
only to initiating beacons — answering a response would ping-pong forever. Whatever time you spend
between receiving and replying lands in *its* measured RTT, so do it inline: no queue, no batching.

```python
from visio_schema import Message
from visio_schema.v1.service.heartbeat import heartbeat_pb2
from visio_schema.v1.wire.header_pb2 import ControlStream

HEARTBEAT = int(ControlStream.CONTROL_STREAM_HEARTBEAT)

def beacon(ep, *, echo_tx=0, echo_rx=0):
    now = time.monotonic_ns()                       # stamp as LATE as possible
    hb = heartbeat_pb2.Heartbeat(
        tx_mono_ns=now, echo_tx_mono_ns=echo_tx, echo_rx_mono_ns=echo_rx
    )
    msg = Message(stream_id=HEARTBEAT, payload=hb.SerializeToString())
    msg.timestamp.FromNanoseconds(now)
    ep.send(msg)
    return now
```

Note this needs a **bidirectional** connection — `serial_endpoint(path)`, not the read-only
`read_serial(path)`. See [usage.md](usage.md#3-integrate--send-commands).

### b. Initiate — beacon on a timer

Responding alone teaches *you* nothing: the offset is computed by whoever started the round trip.
So beacon on your own timer (1 Hz is the usual cadence; 10 Hz converges faster and costs nothing),
remembering each send so you can recognise the echo when it comes back:

```python
sent = collections.deque(maxlen=64)                  # our recent tx stamps

def beacon_loop(ep, stop, interval_s=1.0):
    while not stop.is_set():
        sent.append(beacon(ep))                      # echo fields 0 ⇒ initiating
        stop.wait(interval_s)
```

### c. Close the loop — and filter

When a beacon comes back carrying one of *your* send stamps, you have all four timestamps of an NTP
exchange. `echo_tx_mono_ns` and your receive time are both in your clock, so their difference is a
clean RTT; `echo_rx_mono_ns` is the device's clock at the moment it received you, which — assuming
the path is symmetric — was the flight **midpoint** on yours:

```python
def on_heartbeat(msg, ep, peer):
    rx = time.monotonic_ns()                         # take the rx stamp first
    hb = heartbeat_pb2.Heartbeat(); hb.ParseFromString(msg.payload)

    if hb.echo_tx_mono_ns == 0:                      # initiating beacon → respond (a)
        beacon(ep, echo_tx=hb.tx_mono_ns, echo_rx=rx)
        return
    if hb.echo_tx_mono_ns not in sent:               # not an echo of ours — ignore
        return
    rtt = rx - hb.echo_tx_mono_ns
    offset = (hb.echo_tx_mono_ns + rtt // 2) - hb.echo_rx_mono_ns    # yours minus theirs
    peer.observe(rtt, offset)                        # → the filter below
```

A single sample is noisy: scheduling, USB turnaround and your own write queue each delay **one leg**
of the trip, and one-way asymmetry is exactly what a midpoint estimate cannot see. So keep a small
window, use the offset of its **lowest-RTT** sample — the trip that queued least is the one whose
midpoint is closest to true — and discard anything over 100 ms as not-a-flight:

```python
class PeerClock:
    def __init__(self, window=8, rtt_max_ns=100_000_000):
        self._samples = collections.deque(maxlen=window)     # (rtt, offset)
        self._rtt_max_ns = rtt_max_ns

    def observe(self, rtt_ns, offset_ns):
        if 0 < rtt_ns <= self._rtt_max_ns:
            self._samples.append((rtt_ns, offset_ns))

    @property
    def offset_ns(self):
        return min(self._samples)[1] if self._samples else None   # lowest-RTT sample
```

That is enough for cross-device sensor alignment. If you need sub-millisecond stability across a
long session, fit a line through the low-RTT samples (`offset = a + b·t`) and evaluate it at *now*
instead of taking a single sample — the slope absorbs the drift between the two crystals. The wire
exchange is identical either way.

### d. Apply it

```python
def on_inbound(msg, ep):
    if msg.stream_id == HEARTBEAT:
        on_heartbeat(msg, ep, peer)
        return
    offset = peer.offset_ns
    if offset is None:
        return                                       # not synced yet — don't guess
    msg.timestamp.FromNanoseconds(msg.timestamp.ToNanoseconds() + offset)
    # msg.timestamp is now YOUR monotonic clock — comparable with your own data.
```

---

## 4. From monotonic to wall clock

Timesync puts everything on your **monotonic** clock, which means nothing outside your process. Two
ways to reach Unix time:

**Live** — sample both clocks together and carry the difference. Once per run, not per message: the
wall clock can step (an NTP correction, a manual set) while the monotonic clock cannot, so
re-sampling would plant that jump in the middle of your data.

```python
_MONO_TO_UNIX_NS = time.time_ns() - time.monotonic_ns()       # measured once, at startup
unix_ns = local_mono_ns + _MONO_TO_UNIX_NS
```

**Offline** — a device reports `DeviceInfo.boot_unix_seconds` (its wall clock at boot) in the
announce that `ChannelRegistry` already consumes, so a recording can be placed on absolute time
after the fact: `unix_ns = boot_unix_seconds * 1e9 + device_mono_ns`. It is `0` on a unit with no
real-time clock, in which case use your own wall clock at session start.

`McapWriter` writes `Header.timestamp` verbatim as MCAP `log_time`, so whichever domain you hand it
is the domain your file is in. Foxglove Studio plays a monotonic-domain file perfectly well; it just
shows dates near 1970.

---

## 5. Checklist

Every item here is a real failure mode, in rough order of how often it bites:

- [ ] **Add**, don't subtract: `local = device + offset` ([§2](#2-the-one-thing-to-get-right-the-sign)).
- [ ] Stamp `tx_mono_ns` as late as possible — at the write, not when you build the message.
      Anything between the stamp and the wire is one-way delay, and it biases the offset by half of
      itself.
- [ ] Answer initiating beacons **inline**. Your latency becomes the device's error.
- [ ] Never answer a response beacon (`echo_tx_mono_ns != 0`) — that's an infinite ping-pong.
- [ ] Ignore an echo whose `echo_tx_mono_ns` isn't one of your sends.
- [ ] Discard `rtt <= 0` and `rtt > 100 ms`; prefer the lowest-RTT sample in a window.
- [ ] Treat "no offset yet" as its own state. Don't emit a rewritten timestamp before the first
      sample lands, and don't read a `0` offset as "converged and perfectly aligned" — across two
      machines a true offset is never exactly zero.
- [ ] Keep one offset per connection, not per device name.
- [ ] Don't rewrite payload-internal times when you rewrite the header — convert those on decode.
- [ ] If you produce Visio messages yourself, `Header.timestamp` is **capture** time, never send
      time. Stamping at send re-introduces exactly the per-frame jitter this mechanism exists to
      remove, and no amount of timesync recovers it.

## 6. Checking that it works

The cheap end-to-end assertion is **message age**: after rewriting, `now − Header.timestamp` is a
real end-to-end latency, so it should be a small positive number of milliseconds and stay there.
A flipped sign shows up as an age of roughly twice the offset (seconds to hours, and often
negative); an unconverged offset shows up as an age equal to the difference in uptimes.

```
stream 16   seq 12043    age    1.83 ms
stream 17   seq 3011     age    2.41 ms
```

For reference: over a USB CDC serial link the RTT is ~1 ms and the filter settles within a handful
of beacons. Over a local loopback with a 12.3 s artificial clock offset, the example client recovers
the offset to within ~10 µs after 3 s of 20 Hz beacons.

If you never get an offset at all:

1. **You're only responding.** Both halves are required — if you never initiate, no round trip is
   ever yours to close, and the offset stays `None` forever while the link looks perfectly healthy
   and heartbeats stream in.
2. **Your beacons aren't reaching the device.** Check you opened a bidirectional endpoint
   (`serial_endpoint`, not `read_serial`) and that `send` is going to the same link you're reading.
3. **Every sample is gated out.** A saturated link — stereo H.265 video over a narrow serial pipe —
   can push every RTT past the 100 ms ceiling. That's the link telling you something; check it
   rather than widening the filter.

## See also

- [`protocol/timesync.md`](protocol/timesync.md) — the normative algorithm and timestamp contract.
- [`proto/visio_schema/v1/service/heartbeat/heartbeat.proto`](../proto/visio_schema/v1/service/heartbeat/heartbeat.proto) — the wire message.
- [`examples/python/timesync_client.py`](../examples/python/timesync_client.py) — the complete client used above.
- [`usage.md`](usage.md) — reading streams and recordings, and sending commands.
