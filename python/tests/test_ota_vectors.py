"""Replay tests/golden/ota_vectors.txt through the Python driver.

This is the Python end of the cross-language pin: the same file is replayed by
cpp/tests/test_ota_vectors.cc, so a rule that drifts between the two shows up
here as a byte diff rather than on a board.

It is deliberately NOT a restatement of test_wire_ota.py. That file says what the
rules ARE; this one says all three implementations agree on them.
"""
import struct
import sys
from pathlib import Path

import pytest

from visio_schema.wire import ota

# tests/ is not a package (a sibling suite already trips over that), so reach
# the shared loader by path rather than by relative import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _golden  # noqa: E402

V = _golden.load("ota_vectors.txt")
CASES = sorted({k.split(".")[0] for k in V})


def image(n):
    return bytes(((i * 31 + 7) & 0xFF) for i in range(n))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class Replay:
    """The scripted peer: every send must match, and its replies then land."""

    def __init__(self, case, clock):
        self.case, self.clock, self.n = case, clock, 0
        self.outbox = []

    def send(self, payload):
        want = V.get(f"{self.case}.{self.n:02d}.send")
        assert want is not None, (
            f"{self.case}: sent {self.n + 1} messages, the vector has {self.n}")
        assert payload == want, (
            f"{self.case} step {self.n}: driver emitted a different OtaMessage\n"
            f"  want {want.hex()}\n  got  {payload.hex()}")
        i = 0
        while (r := V.get(f"{self.case}.{self.n:02d}.reply.{i:02d}")) is not None:
            self.outbox.append(r)
            i += 1
        self.n += 1

    def recv(self, timeout):
        if self.outbox:
            return self.outbox.pop(0)
        if timeout:
            self.clock.sleep(timeout)
        return None


@pytest.mark.parametrize("case", CASES)
def test_the_driver_reproduces_the_transcript(case):
    (total, chunk, window, cap, session, _rsv0, negotiate,
     commit, _rsv1) = struct.unpack(">QIIIQBBBB", V[f"{case}.params"])
    clock = Clock()
    dev = Replay(case, clock)
    out = ota.relay(
        dev.send, dev.recv, image(total),
        fw_version=V[f"{case}.fw"].decode(),
        board=V[f"{case}.board"].decode(),
        target_device=V[f"{case}.target"].decode(),
        session_id=session, commit=bool(commit),
        window=window, chunk=chunk, chunk_cap=cap,
        negotiate=bool(negotiate), clock=clock)

    assert V.get(f"{case}.{dev.n:02d}.send") is None, (
        f"{case}: the vector expects more messages than the driver sent")
    ok, reason, acked, want_total, resumes = struct.unpack(
        ">BBQQI", V[f"{case}.out"])
    assert (int(out.ok), int(out.reason), out.acked, out.total, out.resumes) == (
        ok, reason, acked, want_total, resumes)


def test_the_vector_file_covers_the_cases_that_matter():
    """A transcript set that quietly loses a case still passes every case it
    kept, so pin the roster itself."""
    assert set(CASES) >= {"happy_advert", "happy_legacy", "instant_revert",
                          "wrong_board", "addressed_leaf", "no_flash"}
