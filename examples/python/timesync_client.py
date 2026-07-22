#!/usr/bin/env python3
"""Sync a client's clock to a Visio device — the heartbeat NTP loop, standalone.

A device stamps every `Header.timestamp` in ITS OWN monotonic clock, which shares
no epoch with yours. To place its samples on your timeline you need the offset
between the two clocks, and Visio carries that in the heartbeat beacon: an
NTP-style round trip on the `CONTROL_STREAM_HEARTBEAT` control stream (spec:
``docs/protocol/timesync.md``, how-to: ``docs/timesync_client.md``).

The whole exchange is four small pieces:

    responder  — reply to the device's beacons so it can sync to you.
    initiator  — beacon on a timer; the device echoes; close the loop.
    filter     — keep the lowest-RTT offset in a sliding window.
    apply      — offset + device_ns = your clock.

Run it against a device (Ctrl-C to stop)::

    python examples/python/timesync_client.py /dev/ttyACM0
"""
from __future__ import annotations

import argparse
import threading
import time
from collections import deque

from visio_schema import Message, serial_endpoint
from visio_schema.transport import Endpoint
from visio_schema.v1.service.heartbeat import heartbeat_pb2
from visio_schema.v1.wire.header_pb2 import ControlStream

HEARTBEAT = int(ControlStream.CONTROL_STREAM_HEARTBEAT)

_RTT_MAX_NS = 100_000_000     # 100 ms — above this the sample is queueing, not flight
_WINDOW = 8                   # samples kept for the min-RTT selection


class PeerClock:
    """Sliding-window estimate of (our clock minus the peer's clock), in nanoseconds.

    A single round trip is noisy — scheduling, USB turnaround and outbox queueing
    all land on one leg of the path and bias the midpoint estimate. Keeping a
    window and reporting the offset of its **lowest-RTT** sample takes the round
    trip that queued least, which is the one whose midpoint is closest to true.
    """

    def __init__(self, window: int = _WINDOW, rtt_max_ns: int = _RTT_MAX_NS) -> None:
        self._samples: deque[tuple[int, int]] = deque(maxlen=window)   # (rtt, offset)
        self._rtt_max_ns = rtt_max_ns

    def observe(self, rtt_ns: int, offset_ns: int) -> None:
        """Feed one closed round trip. Out-of-range RTTs are discarded as outliers."""
        if 0 < rtt_ns <= self._rtt_max_ns:
            self._samples.append((rtt_ns, offset_ns))

    @property
    def offset_ns(self) -> int | None:
        """Offset to ADD to a peer timestamp to bring it into our clock. None until synced."""
        if not self._samples:
            return None
        return min(self._samples)[1]        # offset of the lowest-RTT sample

    @property
    def best_rtt_ns(self) -> int | None:
        return min(self._samples)[0] if self._samples else None


class Timesync:
    """Both halves of the beacon exchange over one `Endpoint`, plus the offset.

    Feed every inbound message to `handle`; it consumes heartbeats and leaves the
    rest to you. `start` also beacons on a timer, which is what makes the device
    echo back and the offset converge.
    """

    def __init__(self, ep: Endpoint, interval_s: float = 1.0) -> None:
        self._ep = ep
        self._interval_s = interval_s
        self._peer = PeerClock()
        self._sent: deque[int] = deque(maxlen=64)   # our recent tx stamps, to match echoes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._beacon_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _beacon_loop(self) -> None:
        while not self._stop.is_set():
            self._beacon(echo_tx=0, echo_rx=0)      # initiating beacon: no echo fields
            self._stop.wait(self._interval_s)

    # ── the exchange ─────────────────────────────────────────────────────
    def _beacon(self, *, echo_tx: int, echo_rx: int) -> None:
        # Stamp as late as possible: everything between here and the wire is
        # one-way delay that inflates the measured RTT on this leg only.
        now = time.monotonic_ns()
        hb = heartbeat_pb2.Heartbeat(
            tx_mono_ns=now, echo_tx_mono_ns=echo_tx, echo_rx_mono_ns=echo_rx
        )
        msg = Message(stream_id=HEARTBEAT, payload=hb.SerializeToString())
        msg.timestamp.FromNanoseconds(now)
        if echo_tx == 0:
            self._sent.append(now)
        self._ep.send(msg)

    def handle(self, msg: Message) -> bool:
        """Process one inbound message. Returns True if it was a heartbeat (consumed)."""
        if msg.stream_id != HEARTBEAT:
            return False
        rx = time.monotonic_ns()                    # take the rx stamp first
        hb = heartbeat_pb2.Heartbeat()
        hb.ParseFromString(msg.payload)

        if hb.echo_tx_mono_ns == 0:
            # Initiating beacon from the device: reply at once, echoing its send
            # time and our receive time. Any delay here lands in ITS RTT.
            self._beacon(echo_tx=hb.tx_mono_ns, echo_rx=rx)
            return True

        # A response to one of our beacons — close the loop.
        if hb.echo_tx_mono_ns not in self._sent:
            return True                             # not ours (or too old): ignore
        rtt = rx - hb.echo_tx_mono_ns               # both stamps are in OUR clock
        # The device received our beacon at echo_rx_mono_ns (its clock). Assuming a
        # symmetric path, that same instant was (tx + rtt/2) on ours, so:
        offset = (hb.echo_tx_mono_ns + rtt // 2) - hb.echo_rx_mono_ns   # ours minus theirs
        self._peer.observe(rtt, offset)
        return True

    # ── using the result ─────────────────────────────────────────────────
    @property
    def offset_ns(self) -> int | None:
        """Offset to ADD to a device timestamp to get our clock. None until synced."""
        return self._peer.offset_ns

    @property
    def best_rtt_ns(self) -> int | None:
        return self._peer.best_rtt_ns

    def to_local_ns(self, peer_ns: int) -> int | None:
        """Convert a device-clock timestamp (ns) to our monotonic clock."""
        offset = self.offset_ns
        return None if offset is None else peer_ns + offset

    def rewrite(self, msg: Message) -> bool:
        """Shift `msg.timestamp` from the device's clock into ours, in place.

        This is the whole point of the exchange: afterwards the header is
        comparable with your own data. Payload-internal times
        (``ImuRaw.first_sample_time``, ``CompressedVideo.timestamp``, …) are NOT
        touched — convert those with `to_local_ns` when you decode them.
        """
        local = self.to_local_ns(msg.timestamp.ToNanoseconds())
        if local is None:
            return False
        msg.timestamp.FromNanoseconds(local)
        return True


# Sampled ONCE, at import. The wall clock can step (an NTP correction, a manual
# set) while the monotonic clock cannot, so re-reading it per message would plant
# that jump in the middle of a recording.
_MONO_TO_UNIX_NS = time.time_ns() - time.monotonic_ns()


def monotonic_to_unix_ns(mono_ns: int) -> int:
    """Map a local monotonic timestamp onto the wall clock (for MCAP log_time, UIs)."""
    return mono_ns + _MONO_TO_UNIX_NS


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("port", nargs="?", default="/dev/ttyACM0", help="serial port")
    ap.add_argument("--interval", type=float, default=1.0, help="beacon period (s)")
    args = ap.parse_args()

    ep = serial_endpoint(args.port)
    sync = Timesync(ep, interval_s=args.interval)

    def on_inbound(msg: Message, _ep: Endpoint) -> None:
        if sync.handle(msg):                    # heartbeat: consumed by the sync
            return
        if not sync.rewrite(msg):               # data: shift into our clock
            return                              # not synced yet — skip it
        # msg.timestamp is now our monotonic clock, so this age is a real
        # end-to-end latency. A wrong-sign offset shows up here as a wild value.
        age_ms = (time.monotonic_ns() - msg.timestamp.ToNanoseconds()) / 1e6
        print(f"stream {msg.stream_id:<4} seq {msg.seq:<8} age {age_ms:7.2f} ms")

    ep.start(on_inbound, None)
    sync.start()
    print(f"beaconing on {args.port} every {args.interval}s — waiting for sync…")
    try:
        while True:
            time.sleep(2.0)
            offset, rtt = sync.offset_ns, sync.best_rtt_ns
            if offset is None:
                print("not synced yet (no echo received)")
            else:
                print(f"offset {offset / 1e6:+.3f} ms   best rtt {rtt / 1e6:.3f} ms")
    except KeyboardInterrupt:
        pass
    finally:
        sync.stop()
        ep.stop()


if __name__ == "__main__":
    main()
