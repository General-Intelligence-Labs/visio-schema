"""Guard the timesync client example — the recipe in docs/timesync_client.md.

The example is what an integrator copies, so what it pins is the part that is silently
wrong when you get it wrong: the **sign** of the offset (add ours-minus-theirs to a
device timestamp), the responder leg (answer initiating beacons, never response ones),
and the min-RTT filter. A flipped sign still produces plausible timestamps — twice the
offset away — so only an end-to-end check like the pty case below catches it.
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

from visio_schema import Message
from visio_schema.transport import FramedFdEndpoint, make_fd_pair
from visio_schema.v1.service.heartbeat import heartbeat_pb2

_THIS = Path(__file__).resolve().parent
_EXAMPLE = _THIS.parents[1] / "examples" / "python" / "timesync_client.py"


def _load():
    spec = importlib.util.spec_from_file_location("timesync_client", _EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ts = _load()


class _FakeEndpoint:
    """Collects sent messages instead of writing them; enough for the exchange logic."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def start(self, on_inbound, on_closed) -> None: ...

    def send(self, msg: Message) -> None:
        self.sent.append(msg)

    def stop(self) -> None: ...


def _beacon(*, tx: int, echo_tx: int = 0, echo_rx: int = 0) -> Message:
    hb = heartbeat_pb2.Heartbeat(
        tx_mono_ns=tx, echo_tx_mono_ns=echo_tx, echo_rx_mono_ns=echo_rx
    )
    msg = Message(stream_id=ts.HEARTBEAT, payload=hb.SerializeToString())
    msg.timestamp.FromNanoseconds(tx)
    return msg


# ── the filter ────────────────────────────────────────────────────────────


def test_peer_clock_reports_lowest_rtt_sample() -> None:
    pc = ts.PeerClock()
    assert pc.offset_ns is None                      # unsynced is None, NOT 0
    pc.observe(5_000_000, 111)
    pc.observe(1_000_000, 222)                       # lowest RTT → its offset wins
    pc.observe(9_000_000, 333)
    assert pc.offset_ns == 222
    assert pc.best_rtt_ns == 1_000_000


def test_peer_clock_discards_outliers() -> None:
    pc = ts.PeerClock()
    pc.observe(200_000_000, 1)                       # > 100 ms: queueing, not flight
    pc.observe(0, 2)                                 # non-positive RTT is impossible
    pc.observe(-5, 3)
    assert pc.offset_ns is None


# ── the exchange ──────────────────────────────────────────────────────────


def test_answers_an_initiating_beacon() -> None:
    ep = _FakeEndpoint()
    sync = ts.Timesync(ep)
    assert sync.handle(_beacon(tx=1234)) is True     # consumed as a heartbeat
    assert len(ep.sent) == 1

    reply = heartbeat_pb2.Heartbeat()
    reply.ParseFromString(ep.sent[0].payload)
    assert reply.echo_tx_mono_ns == 1234             # echoes the peer's send…
    assert reply.echo_rx_mono_ns > 0                 # …and stamps our receive
    assert reply.tx_mono_ns > 0


def test_never_answers_a_response_beacon() -> None:
    """Replying to a response would ping-pong forever between the two peers."""
    ep = _FakeEndpoint()
    sync = ts.Timesync(ep)
    sync.handle(_beacon(tx=99, echo_tx=time.monotonic_ns(), echo_rx=5))
    assert ep.sent == []


def test_ignores_an_echo_we_never_sent() -> None:
    ep = _FakeEndpoint()
    sync = ts.Timesync(ep)
    sync.handle(_beacon(tx=99, echo_tx=1, echo_rx=2))   # echo_tx isn't one of our sends
    assert sync.offset_ns is None


def test_monotonic_maps_onto_the_wall_clock() -> None:
    """The mono→Unix bridge is sampled once, so it must still track the wall clock."""
    assert abs(ts.monotonic_to_unix_ns(time.monotonic_ns()) - time.time_ns()) < 1_000_000_000


def test_ignores_non_heartbeat_streams() -> None:
    sync = ts.Timesync(_FakeEndpoint())
    assert sync.handle(Message(stream_id=16, payload=b"data")) is False


def test_offset_sign_from_one_closed_loop() -> None:
    """The peer clock runs D ahead of ours ⇒ offset must be about -D (ours minus theirs)."""
    ep = _FakeEndpoint()
    sync = ts.Timesync(ep)
    d = 12_345_678_901                                # peer clock = ours + D

    sync._beacon(echo_tx=0, echo_rx=0)                # our initiating beacon
    ours_tx = heartbeat_pb2.Heartbeat.FromString(ep.sent[0].payload).tx_mono_ns
    # The peer receives it a moment later, on its own clock, and echoes at once.
    sync.handle(_beacon(tx=ours_tx + d, echo_tx=ours_tx, echo_rx=ours_tx + d))

    assert sync.offset_ns is not None
    assert abs(sync.offset_ns + d) < 10_000_000       # within 10 ms of -D
    # And that offset moves a peer timestamp onto our clock.
    peer_now = time.monotonic_ns() + d
    assert abs(sync.to_local_ns(peer_now) - time.monotonic_ns()) < 10_000_000


# ── end to end over a pty ─────────────────────────────────────────────────


@pytest.mark.pty  # pty readability isn't signaled on macOS — see tests/conftest.py
def test_converges_and_rewrites_over_a_link() -> None:
    """Full loop against a simulated device whose clock is D ahead of ours."""
    d = 12_345_678_901
    master, slave = make_fd_pair()
    dev, cli = FramedFdEndpoint(master), FramedFdEndpoint(slave)

    def dev_now() -> int:
        return time.monotonic_ns() + d

    def dev_inbound(msg: Message, _ep) -> None:
        if msg.stream_id != ts.HEARTBEAT:
            return
        rx = dev_now()
        hb = heartbeat_pb2.Heartbeat.FromString(msg.payload)
        if hb.echo_tx_mono_ns == 0:                   # answer the client's beacon
            dev.send(_beacon(tx=dev_now(), echo_tx=hb.tx_mono_ns, echo_rx=rx))

    ages: list[float] = []

    def cli_inbound(msg: Message, _ep) -> None:
        if sync.handle(msg) or not sync.rewrite(msg):
            return
        ages.append((time.monotonic_ns() - msg.timestamp.ToNanoseconds()) / 1e6)

    sync = ts.Timesync(cli, interval_s=0.05)
    try:
        dev.start(dev_inbound, None)
        cli.start(cli_inbound, None)
        sync.start()

        deadline = time.monotonic() + 10.0
        while sync.offset_ns is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sync.offset_ns is not None, "never converged"
        assert abs(sync.offset_ns + d) < 20_000_000   # within 20 ms of the true -D

        # A data message stamped in the device's clock lands back on ours.
        data = Message(stream_id=16, payload=b"\x00" * 8)
        data.timestamp.FromNanoseconds(dev_now())
        dev.send(data)
        deadline = time.monotonic() + 5.0
        while not ages and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ages, "no data message was rewritten"
        assert abs(ages[0]) < 100.0                   # ms of end-to-end age, not seconds
    finally:
        sync.stop()
        cli.stop()
        dev.stop()
