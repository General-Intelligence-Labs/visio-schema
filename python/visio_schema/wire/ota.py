"""Drive a firmware OTA over the Visio bus. Owns no connection.

The host is a **blind relay**: only the device decrypts the bundle with its
baked-in bundle key and verifies it, so this module never looks inside the
payload — it streams an opaque envelope and reads back ``OtaStatus``.

Transport-agnostic on purpose. `relay` takes a ``send``/``recv`` pair, so the
same state machine drives a raw socket, a serial port, or a live ``visio`` bus
leg. That keeps this repo a contract repo: it says how to speak OTA, not how to
reach a device.

The flow control here was proven against real hardware. The comments explaining
*why* each rule exists are the reason it works, not decoration.
"""
from __future__ import annotations

import enum
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, Union

from visio_schema.v1.service.ota import ota_pb2

__all__ = [
    "RIG_TERMINAL_SESSION",
    "MIN_CHUNK_BYTES",
    "QUERY_WAIT_S",
    "ImageSource",
    "Reason",
    "negotiate_chunk",
    "next_session_id",
    "query_message",
    "COMMIT_WAIT_S",
    "DEADLINE_S",
    "DEFAULT_SESSION_ID",
    "MAX_CHUNK_BYTES",
    "RKFW_MAGIC",
    "SOFT_RETRY_S",
    "STALL_TIMEOUT_S",
    "TCP_CHUNK_BYTES",
    "TCP_WINDOW_BYTES",
    "USB_CHUNK_BYTES",
    "USB_WINDOW_BYTES",
    "VENC_MAGIC",
    "Outcome",
    "Progress",
    "abort_message",
    "begin_message",
    "bundle_error",
    "chunk_message",
    "commit_message",
    "relay",
]

VENC_MAGIC, RKFW_MAGIC = b"VENC", b"RKFW"

#: One relay owns one session; the device keys its staging on it.
DEFAULT_SESSION_ID = 0xCA11

#: The reserved session id a head publishes a whole RIG's verdict on. The device
#: firmware reserves the same id: a rig update is several transfers plus several
#: reboots, and its SUCCESS/FAILED belongs to none of them, so it carries this
#: session rather than any one transfer's. A relay folds only its own session
#: (see `relay`), so a rig verdict never lands in a transfer.
#:
#: Named RIG, not BUNDLE, and the rename is the point: "bundle" already means
#: the ENCRYPTED IMAGE everywhere else in this stack (`bundle_key`,
#: `/etc/ota_bundle.key`, `bundle_error`), so using it for the TRANSACTION too
#: made two different things share one word in the same file. The firmware
#: spells it `kRigTerminalSession`. The NUMBER is the contract; the name is not.
RIG_TERMINAL_SESSION = 0xB1D

# TCP has no gadget-FIFO limit — the kernel socket buffer absorbs the device's
# NAND-write stalls. 32 KiB is the size every fielded device has always
# accepted; a device that can take more says so in OtaStatus.max_chunk_bytes.
#
# The hard ceiling is MAX_CHUNK_BYTES, and it is NOT frame reassembly as this
# comment once claimed — it is nanopb's size type. See ota.proto's
# OtaChunk.data.
TCP_WINDOW_BYTES = 2 * 1024 * 1024
TCP_CHUNK_BYTES = 32 * 1024

# OtaChunk.data is a nanopb bytes array and nanopb is built without
# PB_FIELD_32BIT, so pb_size_t is 16-bit and PB_SIZE_MAX is 65535. A larger
# chunk cannot be pb_decode'd at all: the device answers every frame with
# "OtaMessage decode failed" and the transfer never advances, which reads like a
# dead link rather than a size problem. A sender MUST clamp to this rather than
# trust a device's advertisement. Headroom left for the fields wrapping the
# payload. Confirmed on hardware: a large image at 256 KiB chunks moves nothing.
MAX_CHUNK_BYTES = 60 * 1024

# USB CDC-ACM: total in-flight must stay <= the device's gadget RX FIFO (a few
# KB). The device decrypts and writes each chunk to NAND inline on its single I/O
# thread, so while it stalls in a flash write it stops reading, and a too-large
# window overruns the FIFO -> dropped bytes -> COBS CRC fail -> "gap in image
# stream".
USB_WINDOW_BYTES = 8 * 1024
USB_CHUNK_BYTES = 4 * 1024

#: Floor for a negotiated chunk. Mirrors OtaManager::kMinAckIntervalBytes: the
#: device paces its RECEIVING acks off OtaBegin.chunk_bytes, and those acks are
#: the sender's window credit, so a tiny chunk starves the window rather than
#: making the transfer safer. A device advertising less than this is clamped UP.
MIN_CHUNK_BYTES = 8 * 1024

#: How long to wait for the OtaStatus answering the pre-Begin OtaQuery. Bounded
#: and short: any status ends it, and firmware predating max_chunk_bytes simply
#: never answers, which is a legitimate outcome (keep the caller's default), not
#: an error worth stalling a push over.
QUERY_WAIT_S = 1.5

STALL_TIMEOUT_S = 45.0
SOFT_RETRY_S = 6.0
COMMIT_WAIT_S = 8.0
DEADLINE_S = 900.0


class Reason(enum.IntEnum):
    """Why a relay ended, as a CLOSED set.

    ``Outcome.detail`` is prose for humans and is free to be reworded; this is
    the machine-readable half, and it is what the cross-language conformance
    vectors pin. Three implementations cannot be held to a sentence.

    The numeric values are wire-visible (the vectors carry them as one byte), so
    they are append-only: never renumber, never reuse.
    """

    OK_STAGED = 0
    OK_SUCCESS = 1
    OK_STAGED_NO_TRANSFER = 2      # A/B instant revert: staged without bytes
    OK_COMMITTED_UNCONFIRMED = 3   # STAGED ack raced the apply reboot
    OK_LINK_AFTER_COMMIT = 4       # link dropped after commit, device rebooting
    FAIL_DEVICE = 5                # the device said FAILED
    FAIL_STALLED = 6
    FAIL_DEADLINE = 7
    FAIL_TOO_LOSSY = 8
    FAIL_LINK_DROPPED = 9          # link gone mid-transfer, nothing committed
    OK_UPLOAD_ONLY = 13            # every byte landed, then aborted on purpose
    # 10, 11 and 12 are RETIRED, not free. They belonged to the two-phase hold
    # a pusher no longer has (FAIL_HELD_UNCONFIRMED, OK_APPLYING,
    # FAIL_APPLY_REFUSED). Append-only means never REUSE, not never remove.


@dataclass(frozen=True)
class Progress:
    """A point-in-time view, handed to ``on_progress``."""

    sent: int
    acked: int
    total: int
    resumes: int
    state: str


@dataclass(frozen=True)
class Outcome:
    ok: bool
    detail: str
    acked: int
    total: int
    resumes: int
    #: The closed-set half of `detail`. Defaulted so a caller constructing an
    #: Outcome by hand (tests, fakes) keeps working.
    reason: Reason = Reason.FAIL_DEVICE


def bundle_error(raw: bytes) -> str | None:
    """Why ``raw`` is not an OTA bundle, or None if it is one.

    Two shapes are bundles: a bare VENC envelope (one board -- what every
    fielded ego has always been sent) and a multi-board PACKAGE (see
    `visio_schema.wire.package`). The device dispatches on the same distinction,
    so a pusher must not be stricter than the device it is pushing to.

    Returns a string rather than raising: the caller owns the remedy, which
    differs by context (a GUI logs it, a CLI prints the wrap command).
    """
    from visio_schema.wire import package  # local: package imports nothing heavy
    if package.is_package(raw):
        return None
    if raw[:4] == VENC_MAGIC:
        return None
    if raw[:4] == RKFW_MAGIC:
        return ("a raw RKFW update.img, not an OTA bundle — wrap it first with "
                "scripts/ota_release.py (--image … --version … --board … "
                "--bundle-key …)")
    return f"not a VENC OTA bundle (magic {raw[:4]!r})"


def _message(session_id: int, target_device: str, **body) -> bytes:
    m = ota_pb2.OtaMessage(session_id=session_id)
    if target_device:
        # A relay that owns a dedicated socket never sets this — the link IS
        # the addressing. On a shared bus leg it is required, or the
        # device cannot tell the message was meant for it.
        m.target_device = target_device
    if "begin" in body:
        # Keyword fields, not a positional tuple: fw_version and board are
        # adjacent strings, and a transposition would send a plausible-looking
        # begin that stamps the version as the board.
        m.begin.CopyFrom(ota_pb2.OtaBegin(**body["begin"]))
    elif "chunk" in body:
        m.chunk.offset, m.chunk.data = body["chunk"]
    elif "commit" in body:
        m.commit.SetInParent()
    elif "abort" in body:
        m.abort.reason = body["abort"]
    elif "query" in body:
        m.query.SetInParent()
    return m.SerializeToString()


def begin_message(total_bytes, chunk_bytes, fw_version, board, *,
                  session_id=DEFAULT_SESSION_ID, target_device="") -> bytes:
    # No hold_apply. A pusher cannot ask a device to stage-and-wait: a commit
    # applies, on a limb exactly as on a single board. The two-phase hold existed
    # to shrink the mixed-version window when the APP pushed to each board over a
    # phone link; a hub now holds every image locally before it pushes any, so
    # that window is seconds and the revert path -- which has to be bulletproof
    # anyway -- covers it. A mechanism that keeps the common failures away from
    # the revert path only leaves it untested.
    begin = dict(total_bytes=total_bytes, chunk_bytes=chunk_bytes,
                 fw_version=fw_version, board=board)
    return _message(session_id, target_device, begin=begin)


def chunk_message(offset, data, *, session_id=DEFAULT_SESSION_ID,
                  target_device="") -> bytes:
    return _message(session_id, target_device, chunk=(offset, data))


def commit_message(*, session_id=DEFAULT_SESSION_ID, target_device="") -> bytes:
    return _message(session_id, target_device, commit=True)


def abort_message(reason, *, session_id=DEFAULT_SESSION_ID,
                  target_device="") -> bytes:
    return _message(session_id, target_device, abort=reason)


def query_message(*, session_id=DEFAULT_SESSION_ID, target_device="") -> bytes:
    """Ask the device for a status without opening a session.

    Its answer carries ``max_chunk_bytes``, which ota.proto requires a sender to
    read BEFORE OtaBegin: ``chunk_bytes`` is both the frame size and the device's
    ack cadence, so it cannot be renegotiated once the session is open.
    """
    return _message(session_id, target_device, query=True)


_session_seq = itertools.count(1)


def next_session_id() -> int:
    """A session id distinct from this process's other pushes.

    `DEFAULT_SESSION_ID` is a fixed constant, which is right for a laptop driving
    one board and wrong the moment one process drives SEVERAL — a hub updating
    two limbs concurrently would have both transfers folding each other's
    statuses. Seeded off the clock so two processes on one bus rarely collide.
    """
    return (int(time.time() * 1000) & 0xFFFFFFFF) << 16 | (
        next(_session_seq) & 0xFFFF)


class ImageSource(Protocol):
    """Random access to a bundle too big to hold in memory.

    A rig package is hundreds of MB and neither a phone nor an RV1126B head can
    buffer one, so `relay` takes either ``bytes`` or this.
    """

    def __len__(self) -> int: ...

    def read(self, offset: int, length: int) -> bytes: ...


Image = Union[bytes, ImageSource]


def _slice(image: Image, offset: int, length: int) -> bytes:
    if isinstance(image, (bytes, bytearray, memoryview)):
        return bytes(image[offset:offset + length])
    return image.read(offset, length)


def negotiate_chunk(send: Callable[[bytes], None],
                    recv: Callable[[float], bytes | None],
                    *, want: int, cap: int = 0,
                    session_id: int = DEFAULT_SESSION_ID,
                    target_device: str = "",
                    query_wait: float = QUERY_WAIT_S,
                    clock: Callable[[], float] = time.monotonic) -> int:
    """The chunk size to send, after asking the device what it can take.

    ota.proto: a sender uses ``min(its own CEILING, advertised)`` — not its own
    default, which would make the field a no-op.

    Four rules, each of which lived in exactly one implementation before this:

    * ``0`` advertised, or no answer at all, means firmware predating the field:
      keep ``want`` UNTOUCHED. `MIN_CHUNK_BYTES` and `MAX_CHUNK_BYTES` bound what
      a DEVICE may talk us into, never what the caller asked for — `USB_CHUNK_BYTES`
      is deliberately below the floor, and flooring it would break the CDC-ACM leg.
    * an advert WINS over ``want``, up or down. Taking ``min(want, advertised)``
      would make the field a no-op for every device that can take more than the
      32 KiB default, which is most of them.
    * take the SMALLEST advert across everyone who answers. A begin addressed to
      a board CLASS reaches several units — an eMMC head and two NAND hands — and
      one image means one chunk size, so it must fit the most constrained board.
    * ``cap`` is applied LAST and to everything: it is the LINK's limit (the
      CDC-ACM gadget RX FIFO), which the device cannot see and so cannot report.
    """
    send(query_message(session_id=session_id, target_device=target_device))
    smallest = 0
    deadline = clock() + query_wait
    while clock() < deadline:
        raw = recv(max(0.0, deadline - clock()))
        if raw is None:
            continue
        s = ota_pb2.OtaStatus()
        s.ParseFromString(raw)
        if s.session_id and s.session_id != session_id:
            continue
        if s.max_chunk_bytes:
            smallest = (s.max_chunk_bytes if smallest == 0
                        else min(smallest, s.max_chunk_bytes))
    out = (want if smallest == 0
           else max(MIN_CHUNK_BYTES, min(smallest, MAX_CHUNK_BYTES)))
    return min(out, cap) if cap else out


def relay(send: Callable[[bytes], None],
          recv: Callable[[float], bytes | None],
          image: Image, *, fw_version: str, board: str = "",
          target_device: str = "", session_id: int = DEFAULT_SESSION_ID,
          window: int = TCP_WINDOW_BYTES, chunk: int = TCP_CHUNK_BYTES,
          chunk_cap: int = 0, negotiate: bool = True, commit: bool = True,
          query_wait: float = QUERY_WAIT_S,
          stall_timeout: float = STALL_TIMEOUT_S,
          soft_retry: float = SOFT_RETRY_S,
          commit_wait: float = COMMIT_WAIT_S,
          deadline_s: float = DEADLINE_S,
          max_resumes: int | None = None,
          on_progress: Callable[[Progress], None] | None = None,
          clock: Callable[[], float] = time.monotonic) -> Outcome:
    """Stream ``image`` to a device: begin -> windowed, RESUME-aware chunks ->
    commit, paced by ``OtaStatus.bytes_received``.

    ``ok`` is True on STAGED/SUCCESS **or** a post-commit link drop — the device
    reboots to apply, and its STAGED ack routinely races that drop.

    Args:
        send: put one serialized OtaMessage on the device's OTA stream. Raises
            ``ConnectionError`` when the link is gone.
        recv: next ``OtaStatus`` payload, or None after ``timeout`` seconds.
        image: the opaque VENC bundle (see `bundle_error`).
        board: the IMAGE's target board, never the device's — the unit compares
            it against its own hardware_revision, so stamping it from what we
            read OFF the device would make the check a tautology.
        deadline_s: absolute cap on the whole transfer. The stall timer only
            catches a device that stops acking; a device trickling one ack just
            under ``stall_timeout`` would otherwise run forever.
        commit: False uploads every byte and then ABORTS instead of
            committing — proves the transfer path without flashing anything.
            A bench verb, and the one way to exercise a push against a unit you
            are not willing to reboot.
    """
    OS = ota_pb2.OtaStatus
    total = len(image)
    st = {"acked": 0, "staged": False, "succeeded": False, "failed": None,
          "resume_to": None}

    def fold(raw: bytes) -> None:
        s = OS()
        s.ParseFromString(raw)
        # A status the device stamps for some OTHER session is not ours to fold:
        # its bytes_received would move our watermark and its FAILED would abort
        # a healthy transfer. Unset (0) is folded — the device leaves it clear on
        # the periodic no-session IDLE, and older firmware never sets it.
        if s.session_id and s.session_id != session_id:
            return
        if s.state == OS.STAGED:                  # armed slot change, rebooting
            st["staged"] = True
            st["acked"] = max(st["acked"], s.bytes_received)
        elif s.state == OS.SUCCESS:
            st["succeeded"] = True
        elif s.state == OS.NEEDS_RESUME:          # gap -> rewind to bytes_received
            if not (st["staged"] or st["succeeded"]):
                st["resume_to"] = s.bytes_received
        elif s.error_code or s.state == OS.FAILED:
            # After staging the device has no session while it reboots, so our
            # last in-flight chunks bounce back as `no_session` FAILEDs — ignore
            # ONLY those; any other post-stage error (e.g. revert_failed) is real.
            if (st["staged"] or st["succeeded"]) and \
                    (not s.error_code or s.error_code == "no_session"):
                pass
            else:
                st["failed"] = (s.error_message or s.error_code
                                or "device reported FAILED")
        else:
            st["acked"] = max(st["acked"], s.bytes_received)

    def pump(timeout: float = 0.0) -> None:
        """Fold every status available now, blocking up to `timeout` for the first."""
        raw = recv(timeout)
        while raw is not None:
            fold(raw)
            raw = recv(0.0)

    def emit(sent: int, state: str, resumes: int) -> None:
        if on_progress is not None:
            on_progress(Progress(sent=sent, acked=st["acked"], total=total,
                                 resumes=resumes, state=state))

    def abort(reason: str) -> None:                # best-effort
        try:
            send(abort_message(reason, session_id=session_id,
                               target_device=target_device))
        except OSError:          # ConnectionError is a subclass; a serial
            pass                 # or socket transport raises the wider one

    def done(ok, detail, resumes, reason):
        return Outcome(ok=ok, detail=detail, acked=st["acked"], total=total,
                       resumes=resumes, reason=reason)

    committed = False
    cursor = last_ack = last_log = resumes = 0
    derive_resumes = max_resumes is None
    try:
        if negotiate:
            # Before the begin, never during: chunk_bytes is the device's ack
            # cadence as well as the frame size, so it cannot change once the
            # session is open.
            chunk = negotiate_chunk(send, recv, want=chunk, cap=chunk_cap,
                                    session_id=session_id,
                                    target_device=target_device,
                                    query_wait=query_wait, clock=clock)
        elif chunk_cap:
            chunk = min(chunk, chunk_cap)
        send(begin_message(total, chunk, fw_version, board,
                           session_id=session_id, target_device=target_device))
        if derive_resumes:
            max_resumes = 2 * (total // max(chunk, 1) + 2) + 16
        t_start = last_progress = last_soft = clock()
        emit(0, "begin", 0)
        # Read once before streaming: a device whose other slot already holds
        # this build answers STAGED immediately, and a wrong-board refusal comes
        # back just as fast. Both are worth catching before pushing 60 MB.
        pump()

        # Send until the device has CONTIGUOUSLY acked every byte (or staged
        # early), honoring NEEDS_RESUME (rewind) + soft-retry.
        while not st["failed"] and not st["succeeded"] and not st["staged"]:
            if clock() - t_start > deadline_s:
                abort("deadline")
                return done(False, f"exceeded {deadline_s:.0f}s overall deadline "
                                   f"at {st['acked'] // 1024} KiB", resumes,
                            Reason.FAIL_DEADLINE)
            if st["resume_to"] is not None:        # gap -> rewind + resend
                resumes += 1
                if resumes > max_resumes:
                    abort("too lossy")
                    return done(False, f"too lossy: {resumes} resumes at "
                                       f"{st['acked'] // 1024} KiB", resumes,
                                Reason.FAIL_TOO_LOSSY)
                cursor = st["resume_to"]
                st["resume_to"] = None   # NOTE: do NOT reset the stall clock
                continue
            n = min(chunk, total - cursor)
            # Bound TOTAL in-flight (unacked) to `window`. With a non-blocking
            # transport this is the ONLY backpressure, so it must also stay well
            # inside the host outbox depth: window/chunk frames in flight.
            if cursor < total and (cursor - st["acked"]) + n <= window:
                send(chunk_message(cursor, _slice(image, cursor, n),
                                   session_id=session_id,
                                   target_device=target_device))
                cursor += n
                pump()
            elif cursor >= total and st["acked"] >= total:
                break                              # all sent + acked -> commit
            else:
                pump(0.05)
            now = clock()
            if st["acked"] > last_ack:
                last_ack = st["acked"]
                last_progress = last_soft = now
            elif now - last_progress > stall_timeout:
                abort("stall")
                return done(False, f"stalled at {st['acked'] // 1024} KiB "
                                   f"({st['acked'] * 100 // max(total, 1)}%, "
                                   f"{resumes} resumes)", resumes,
                            Reason.FAIL_STALLED)
            elif now - last_soft > soft_retry and st["resume_to"] is None \
                    and cursor > st["acked"]:
                last_soft = now                    # quiet tail -> re-drive from acked
                st["resume_to"] = st["acked"]
            if cursor - last_log >= 4 * 1024 * 1024:
                last_log = cursor
                emit(cursor, "sending", resumes)

        if st["failed"]:
            return done(False, st["failed"], resumes, Reason.FAIL_DEVICE)
        if st["staged"]:                # A/B instant revert: no transfer needed
            return done(True, "staged (no transfer needed)", resumes,
                        Reason.OK_STAGED_NO_TRANSFER)

        if not commit:
            # Everything arrived; deliberately do not flash it. Abort rather
            # than just hanging up, so the device frees its staging now instead
            # of holding a half-session until the next push collides with it.
            abort("no-flash")
            return done(True, "uploaded, not committed (--no-flash)", resumes,
                        Reason.OK_UPLOAD_ONLY)
        # All bytes acked -> commit. The device verifies + reboots; its STAGED ack
        # routinely races the reboot's link drop, so DON'T require it — the
        # caller's post-reboot version check is the real proof.
        emit(cursor, "committing", resumes)
        send(commit_message(session_id=session_id, target_device=target_device))
        committed = True
        deadline = clock() + min(stall_timeout, commit_wait)
        while not (st["staged"] or st["succeeded"] or st["failed"]) \
                and clock() < deadline:
            pump(0.05)
        if st["failed"]:
            return done(False, st["failed"], resumes, Reason.FAIL_DEVICE)
        if st["succeeded"]:
            return done(True, "SUCCESS", resumes, Reason.OK_SUCCESS)
        if st["staged"]:
            return done(True, "STAGED", resumes, Reason.OK_STAGED)
        return done(True, "committed (staging; STAGED ack raced the reboot)",
                    resumes, Reason.OK_COMMITTED_UNCONFIRMED)
    except OSError as e:         # ConnectionError is a subclass. A serial or
        # raw-socket transport raises the wider type, and this module
        # advertises both — catching only ConnectionError would let an
        # OSError escape and replace a clean Outcome with a traceback.
        if st["staged"] or committed:
            return done(True, "link dropped after commit (rebooting to apply)",
                        resumes, Reason.OK_LINK_AFTER_COMMIT)
        return done(False, f"link dropped mid-transfer: {e}", resumes,
                    Reason.FAIL_LINK_DROPPED)

