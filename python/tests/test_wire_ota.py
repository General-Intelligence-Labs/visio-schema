"""The OTA relay state machine, with no socket and no real clock.

Every rule here was proven on hardware in
`visio-setup/calib/push_serial_repush.py`, but several were never covered by a
test on either side — notably the post-stage `no_session` exemption and the
in-flight bound. Those are the ones most likely to be "simplified" away.
"""
from __future__ import annotations

import pytest

from visio_schema.v1.service.ota import ota_pb2
from visio_schema.wire import ota

OS = ota_pb2.OtaStatus
TOTAL = 10_000
CHUNK = 1_000


class Clock:
    """Advances only when the relay waits, so a 45 s stall costs nothing."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class Device:
    """A scriptable OTA peer. `policy` decides what it says to each chunk."""

    def __init__(self, *, total=TOTAL, policy=None, clock=None):
        self.total, self.sent, self.outbox = total, [], []
        self.acked = 0
        self.policy = policy or (lambda dev, msg: dev.ack_contiguous(msg))
        self.clock = clock
        self.dead_after = None
        self.dead_on_recv = False

    # -- transport seam ------------------------------------------------ #
    def send(self, payload):
        if self.dead_after is not None and len(self.sent) >= self.dead_after:
            raise ConnectionError("link gone")
        if self.clock is not None:
            self.clock.sleep(0.01)        # a frame takes time to put on the wire
        m = ota_pb2.OtaMessage()
        m.ParseFromString(payload)
        self.sent.append(m)
        self.policy(self, m)

    def recv(self, timeout):
        if self.dead_on_recv and not self.outbox:
            raise ConnectionError("link gone")
        if self.outbox:
            return self.outbox.pop(0)
        if timeout and self.clock is not None:
            self.clock.sleep(timeout)     # the relay is waiting; let time pass
        return None

    # -- helpers -------------------------------------------------------- #
    def say(self, state, *, acked=None, error_code="", error_message=""):
        s = OS(state=state, bytes_received=self.acked if acked is None else acked,
               error_code=error_code, error_message=error_message)
        self.outbox.append(s.SerializeToString())

    def ack_contiguous(self, m):
        if m.HasField("chunk"):
            if m.chunk.offset == self.acked:      # in-order: advance
                self.acked = m.chunk.offset + len(m.chunk.data)
            self.say(OS.RECEIVING)
        elif m.HasField("commit"):
            self.say(OS.STAGED)

    @property
    def chunks(self):
        return [m for m in self.sent if m.HasField("chunk")]

    @property
    def kinds(self):
        return [m.WhichOneof("body") for m in self.sent]


def run(dev, *, image=None, clock=None, **kw):
    clock = clock or Clock()
    dev.clock = clock
    kw.setdefault("fw_version", "1.2.3")
    return ota.relay(dev.send, dev.recv, image or bytes(dev.total),
                     chunk=CHUNK, window=4 * CHUNK, clock=clock, **kw)


# --- the happy path ------------------------------------------------------ #

def test_streams_every_byte_in_order_exactly_once_then_commits():
    dev = Device()
    image = bytes(range(256)) * (TOTAL // 256) + bytes(TOTAL % 256)
    out = run(dev, image=image)
    assert out.ok and out.detail == "STAGED"
    assert dev.kinds[0] == "begin" and dev.kinds[-1] == "commit"
    rebuilt = b"".join(c.chunk.data for c in dev.chunks)
    assert rebuilt == image
    assert [c.chunk.offset for c in dev.chunks] == list(range(0, TOTAL, CHUNK))


def test_begin_carries_the_image_board_and_version_not_the_devices():
    dev = Device()
    run(dev, fw_version="9.9.9", board="audio_ego_v3")
    b = dev.sent[0].begin
    assert (b.fw_version, b.board) == ("9.9.9", "audio_ego_v3")
    assert (b.total_bytes, b.chunk_bytes) == (TOTAL, CHUNK)


def test_target_device_is_stamped_on_every_frame():
    """push_serial_repush owns a socket so the link is the addressing; on a
    shared bus leg an unstamped frame is a broadcast."""
    dev = Device()
    run(dev, target_device="GILABS-AABBCCDD")
    assert dev.sent and all(m.target_device == "GILABS-AABBCCDD" for m in dev.sent)
    assert all(m.session_id == ota.DEFAULT_SESSION_ID for m in dev.sent)


def test_an_early_staged_skips_the_transfer_entirely():
    """A/B instant revert: the slot already holds this build."""
    def policy(dev, m):
        if m.HasField("begin"):
            dev.say(OS.STAGED)
    dev = Device(policy=policy)
    out = run(dev)
    assert out.ok and out.detail == "staged (no transfer needed)"
    assert not dev.chunks


# --- flow control -------------------------------------------------------- #

def test_in_flight_never_exceeds_the_window():
    """The only backpressure on a non-blocking transport."""
    window, high = 4 * CHUNK, 0

    def policy(dev, m):
        nonlocal high
        if m.HasField("chunk"):
            sent = m.chunk.offset + len(m.chunk.data)
            high = max(high, sent - dev.acked)
            if len(dev.chunks) % 3 == 0:       # ack lazily, in batches
                dev.acked = sent
                dev.say(OS.RECEIVING)
        elif m.HasField("commit"):
            dev.acked = dev.total
            dev.say(OS.STAGED)

    dev = Device(policy=policy)
    assert run(dev).ok
    assert high <= window, f"{high} bytes in flight exceeded the {window} window"


def test_needs_resume_rewinds_and_the_image_still_arrives_whole():
    dropped = {"done": False}

    def policy(dev, m):
        if not m.HasField("chunk"):
            if m.HasField("commit"):
                dev.say(OS.STAGED)
            return
        end = m.chunk.offset + len(m.chunk.data)
        if not dropped["done"] and m.chunk.offset == 3 * CHUNK:
            dropped["done"] = True             # swallow it, then report the gap
            dev.say(OS.NEEDS_RESUME, acked=dev.acked)
            return
        if m.chunk.offset == dev.acked:
            dev.acked = end
        dev.say(OS.RECEIVING)

    dev = Device(policy=policy)
    out = run(dev)
    assert out.ok and out.resumes == 1
    seen = {}
    for c in dev.chunks:
        seen[c.chunk.offset] = c.chunk.data
    assert b"".join(seen[o] for o in sorted(seen)) == bytes(TOTAL)


def test_too_many_resumes_gives_up_and_aborts_on_the_wire():
    def policy(dev, m):
        if m.HasField("chunk"):
            dev.say(OS.NEEDS_RESUME, acked=0)   # never makes progress
    dev = Device(policy=policy)
    out = run(dev, max_resumes=3)
    assert not out.ok and "too lossy" in out.detail
    assert dev.kinds[-1] == "abort" and dev.sent[-1].abort.reason == "too lossy"


def test_a_resume_does_not_reset_the_stall_clock():
    """Else a resume loop masks a wedged device forever."""
    def policy(dev, m):
        if m.HasField("chunk"):
            dev.say(OS.NEEDS_RESUME, acked=0)
    dev = Device(policy=policy)
    out = run(dev, max_resumes=10**6, stall_timeout=5.0)
    assert not out.ok and "stalled" in out.detail


def test_stall_aborts_when_the_device_stops_acking():
    def policy(dev, m):
        if m.HasField("chunk") and m.chunk.offset == 0:
            dev.acked = CHUNK
            dev.say(OS.RECEIVING)              # one ack, then silence
    dev = Device(policy=policy)
    out = run(dev, stall_timeout=30.0)
    assert not out.ok and "stalled at 0 KiB" in out.detail   # 1000 B = 0 KiB
    assert dev.kinds[-1] == "abort" and dev.sent[-1].abort.reason == "stall"


def test_an_overall_deadline_bounds_a_device_that_trickles_acks():
    """The stall timer resets on ANY ack increase, so a slow drip is immortal
    without an absolute cap."""
    def policy(dev, m):
        if m.HasField("chunk") and m.chunk.offset == dev.acked:
            dev.acked += 1                     # 1 byte of progress per chunk
            dev.say(OS.RECEIVING)
    dev = Device(policy=policy)
    out = run(dev, stall_timeout=30.0, deadline_s=10.0)
    assert not out.ok and "deadline" in out.detail
    assert dev.kinds[-1] == "abort" and dev.sent[-1].abort.reason == "deadline"


# --- failure semantics --------------------------------------------------- #

def test_a_device_failure_is_reported_with_its_message():
    def policy(dev, m):
        if m.HasField("begin"):
            dev.say(OS.FAILED, error_code="wrong_board",
                    error_message="image is for ego_v2")
    dev = Device(policy=policy)
    out = run(dev)
    assert not out.ok and out.detail == "image is for ego_v2"


def test_post_stage_no_session_failures_are_ignored():
    """In-flight chunks bounce off a device that already tore its session down."""
    def policy(dev, m):
        if m.HasField("chunk"):
            dev.acked = m.chunk.offset + len(m.chunk.data)
            dev.say(OS.RECEIVING)
        elif m.HasField("commit"):
            dev.say(OS.STAGED)
            dev.say(OS.FAILED, error_code="no_session")   # the tail bouncing
    dev = Device(policy=policy)
    out = run(dev)
    assert out.ok, out.detail


def test_a_real_post_stage_error_is_NOT_ignored():
    def policy(dev, m):
        if m.HasField("chunk"):
            dev.acked = m.chunk.offset + len(m.chunk.data)
            dev.say(OS.RECEIVING)
        elif m.HasField("commit"):
            dev.say(OS.STAGED)
            dev.say(OS.FAILED, error_code="revert_failed")
    dev = Device(policy=policy)
    out = run(dev)
    assert not out.ok and "revert_failed" in out.detail


def test_a_link_drop_AFTER_commit_is_success():
    """The device reboots to apply; its STAGED ack races the drop."""
    def policy(dev, m):
        if m.HasField("chunk"):
            dev.acked = m.chunk.offset + len(m.chunk.data)
            dev.say(OS.RECEIVING)
        elif m.HasField("commit"):
            dev.dead_on_recv = True            # the reboot kills the link
    dev = Device(policy=policy)
    out = run(dev)
    assert out.ok and "rebooting to apply" in out.detail


def test_a_link_drop_BEFORE_commit_is_failure():
    dev = Device()
    dev.dead_after = 3
    out = run(dev)
    assert not out.ok and "link dropped mid-transfer" in out.detail


def test_commit_without_a_staged_ack_still_succeeds():
    def policy(dev, m):
        if m.HasField("chunk"):
            dev.acked = m.chunk.offset + len(m.chunk.data)
            dev.say(OS.RECEIVING)
        # commit: say nothing at all — the reboot ate it
    dev = Device(policy=policy)
    out = run(dev, commit_wait=2.0)
    assert out.ok and "raced the reboot" in out.detail


# --- progress + bundle --------------------------------------------------- #

def test_progress_reports_begin_and_completion():
    seen = []
    dev = Device()
    run(dev, on_progress=seen.append)
    assert seen and seen[0].state == "begin"
    assert seen[-1].state == "committing" and seen[-1].total == TOTAL


@pytest.mark.parametrize("raw,want", [
    (b"VENC\x00\x01", None),
    (b"RKFW\x00\x01", "raw RKFW"),
    (b"\x7fELF\x00", "not a VENC"),
])
def test_bundle_error_names_the_problem(raw, want):
    got = ota.bundle_error(raw)
    assert (got is None) if want is None else (want in got)


# --- the three rules a mutation pass showed were unguarded ---------------- #

def test_the_in_flight_bound_is_never_exceeded():
    """A silent device must not receive more than `window` unacked bytes.

    With a non-blocking transport this bound is the ONLY backpressure, so
    deleting it is invisible on a fast link and overruns the host outbox on a
    slow one. Pinned by count: window/chunk = 4 frames, then the relay waits.
    """
    dev = Device(policy=lambda dev, m: None)      # acks nothing, ever
    out = run(dev)

    # The soft-retry re-drives from `acked` every 6 s, so the chunk COUNT grows
    # until the stall timeout; what must hold is that no chunk is ever sent
    # past the window while nothing has been acked.
    assert {c.chunk.offset for c in dev.chunks} == {0, CHUNK, 2 * CHUNK, 3 * CHUNK}
    assert max(c.chunk.offset + len(c.chunk.data) for c in dev.chunks) == 4 * CHUNK
    assert not out.ok and "stalled at 0 KiB" in out.detail


def test_the_acked_watermark_never_moves_backwards():
    """The device publishes periodic IDLE with `bytes_received=0`.

    Folding that with plain assignment instead of max() rewinds the watermark
    to zero, which jams the in-flight bound and hangs the transfer. The relay
    must keep the high-water mark.
    """
    def ack_then_heartbeat(dev, m):
        dev.ack_contiguous(m)
        dev.say(OS.IDLE, acked=0)                 # a periodic, contentless tick

    dev = Device(policy=ack_then_heartbeat)
    out = run(dev)

    assert out.ok, out.detail
    assert out.acked == TOTAL                     # not reset by the heartbeats
    assert "commit" in dev.kinds


def test_a_final_short_chunk_is_sent_whole():
    """An image that is not a multiple of `chunk` ends in a partial frame."""
    dev = Device(total=TOTAL + 500)
    out = run(dev, image=bytes(TOTAL + 500))

    assert out.ok, out.detail
    assert len(dev.chunks) == 11
    assert len(dev.chunks[-1].chunk.data) == 500  # not padded up to CHUNK
    assert sum(len(c.chunk.data) for c in dev.chunks) == TOTAL + 500
    assert out.acked == TOTAL + 500


def test_a_status_from_another_session_is_ignored():
    """Two sessions can only overlap on a shared leg, but when they do, folding
    the wrong one moves our watermark or aborts a healthy transfer. Unset (0)
    still folds: the device leaves it clear on its no-session IDLE tick."""
    def crosstalk(dev, m):
        dev.ack_contiguous(m)
        s = OS(state=OS.FAILED, session_id=0xBEEF, error_code="not_ours",
               error_message="a different transfer failed")
        dev.outbox.append(s.SerializeToString())

    dev = Device(policy=crosstalk)
    out = run(dev)

    assert out.ok, out.detail                  # the foreign FAILED did not land
    assert out.acked == TOTAL
