"""Drive a firmware OTA over the Visio bus. Owns no connection.

The host is a **blind relay**: only the device decrypts the bundle (AES-256-CTR
with its baked ``/etc/ota_bundle.key``) and verifies it, so this module never
looks inside the payload — it streams an opaque envelope and reads back
``OtaStatus``.

Transport-agnostic on purpose. `relay` takes a ``send``/``recv`` pair, so the
same state machine drives a raw socket, a serial port, or a live ``visio`` bus
leg. That keeps this repo a contract repo: it says how to speak OTA, not how to
reach a device.

Lifted from `visio-setup/calib/push_serial_repush.py::ota_relay`, which proved
the flow control on real hardware. The comments explaining *why* each rule exists
came with it — they are the reason it works, not decoration.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from visio_schema.v1.service.ota import ota_pb2

__all__ = [
    "COMMIT_WAIT_S",
    "DEFAULT_SESSION_ID",
    "RKFW_MAGIC",
    "DEADLINE_S",
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

# TCP has no gadget-FIFO limit — the kernel socket buffer absorbs the device's
# NAND-write stalls. 32 KiB is NOT a tuning choice: larger frames overrun the
# device's frame reassembly and are dropped, yielding zero acks.
TCP_WINDOW_BYTES = 2 * 1024 * 1024
TCP_CHUNK_BYTES = 32 * 1024

# USB CDC-ACM: total in-flight must stay <= the device's gadget RX FIFO (a few
# KB). The device decrypts and writes each chunk to NAND inline on its single I/O
# thread, so while it stalls in a flash write it stops reading, and a too-large
# window overruns the FIFO -> dropped bytes -> COBS CRC fail -> "gap in image
# stream".
USB_WINDOW_BYTES = 8 * 1024
USB_CHUNK_BYTES = 4 * 1024

STALL_TIMEOUT_S = 45.0
SOFT_RETRY_S = 6.0
COMMIT_WAIT_S = 8.0
DEADLINE_S = 900.0


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


def bundle_error(raw: bytes) -> str | None:
    """Why ``raw`` is not an OTA bundle, or None if it is one.

    Returns a string rather than raising: the caller owns the remedy, which
    differs by context (a GUI logs it, a CLI prints the wrap command).
    """
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
        # push_serial_repush never sets this — it owns a dedicated socket, so the
        # link IS the addressing. On a shared bus leg it is required, or the
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
    return m.SerializeToString()


def begin_message(total_bytes, chunk_bytes, fw_version, board, *,
                  session_id=DEFAULT_SESSION_ID, target_device="") -> bytes:
    return _message(session_id, target_device,
                    begin=dict(total_bytes=total_bytes, chunk_bytes=chunk_bytes,
                               fw_version=fw_version, board=board))


def chunk_message(offset, data, *, session_id=DEFAULT_SESSION_ID,
                  target_device="") -> bytes:
    return _message(session_id, target_device, chunk=(offset, data))


def commit_message(*, session_id=DEFAULT_SESSION_ID, target_device="") -> bytes:
    return _message(session_id, target_device, commit=True)


def abort_message(reason, *, session_id=DEFAULT_SESSION_ID,
                  target_device="") -> bytes:
    return _message(session_id, target_device, abort=reason)


def relay(send: Callable[[bytes], None],
          recv: Callable[[float], bytes | None],
          image: bytes, *, fw_version: str, board: str = "",
          target_device: str = "", session_id: int = DEFAULT_SESSION_ID,
          window: int = TCP_WINDOW_BYTES, chunk: int = TCP_CHUNK_BYTES,
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

    def done(ok, detail, resumes):
        return Outcome(ok=ok, detail=detail, acked=st["acked"], total=total,
                       resumes=resumes)

    committed = False
    cursor = last_ack = last_log = resumes = 0
    if max_resumes is None:
        max_resumes = 2 * (total // max(chunk, 1) + 2) + 16
    try:
        send(begin_message(total, chunk, fw_version, board,
                           session_id=session_id, target_device=target_device))
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
                                   f"at {st['acked'] // 1024} KiB", resumes)
            if st["resume_to"] is not None:        # gap -> rewind + resend
                resumes += 1
                if resumes > max_resumes:
                    abort("too lossy")
                    return done(False, f"too lossy: {resumes} resumes at "
                                       f"{st['acked'] // 1024} KiB", resumes)
                cursor = st["resume_to"]
                st["resume_to"] = None   # NOTE: do NOT reset the stall clock
                continue
            n = min(chunk, total - cursor)
            # Bound TOTAL in-flight (unacked) to `window`. With a non-blocking
            # transport this is the ONLY backpressure, so it must also stay well
            # inside the host outbox depth: window/chunk frames in flight.
            if cursor < total and (cursor - st["acked"]) + n <= window:
                send(chunk_message(cursor, image[cursor:cursor + n],
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
                                   f"{resumes} resumes)", resumes)
            elif now - last_soft > soft_retry and st["resume_to"] is None \
                    and cursor > st["acked"]:
                last_soft = now                    # quiet tail -> re-drive from acked
                st["resume_to"] = st["acked"]
            if cursor - last_log >= 4 * 1024 * 1024:
                last_log = cursor
                emit(cursor, "sending", resumes)

        if st["failed"]:
            return done(False, st["failed"], resumes)
        if st["staged"]:                # A/B instant revert: no transfer needed
            return done(True, "staged (no transfer needed)", resumes)

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
            return done(False, st["failed"], resumes)
        if st["succeeded"]:
            return done(True, "SUCCESS", resumes)
        return done(True, "STAGED" if st["staged"]
                    else "committed (staging; STAGED ack raced the reboot)",
                    resumes)
    except OSError as e:         # ConnectionError is a subclass. A serial or
        # raw-socket transport raises the wider type, and this module
        # advertises both — catching only ConnectionError would let an
        # OSError escape and replace a clean Outcome with a traceback.
        if committed or st["staged"]:
            return done(True, "link dropped after commit (rebooting to apply)",
                        resumes)
        return done(False, f"link dropped mid-transfer: {e}", resumes)
