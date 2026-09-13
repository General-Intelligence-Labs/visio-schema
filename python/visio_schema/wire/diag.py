"""Pull a device's diagnostic log over the Visio bus. Owns no connection.

The host is a **blind relay**, as it is for OTA in the other direction: a
``.vdlg`` file is ciphertext under a fleet key the host never holds, and this
module carries the bytes unchanged. Decoding is `visio_schema.diag` /
``visio-diag``, on a machine that has the key.

Transport-agnostic on purpose. `list_files` and `read_file` take a
``send``/``recv`` pair — ``send(raw DiagRequest)`` on CONTROL_STREAM_DIAG and
``recv(timeout) -> raw DiagReply | None`` off the device's ``/<device>/diag``
channel — so one state machine drives a raw socket, a serial port or a live
``visio`` bus leg. The protocol is in ``service/diag/diag.proto``.
"""
from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass

from visio_schema.v1.service.diag import diag_pb2

__all__ = [
    "DEFAULT_SESSION_ID",
    "STALL_TIMEOUT_S",
    "DiagError",
    "FileEntry",
    "abort_message",
    "list_files",
    "list_message",
    "next_session_id",
    "read_file",
    "read_message",
]

#: Every request gets its own session id, counted up from here, and the
#: device echoes it on every reply. Per request, not per connection: a read
#: ends for the host at its last chunk while the device still sends a trailing
#: DONE, and under a shared id that DONE would end the next read empty. Pass
#: ``session_id=`` to pin one instead.
DEFAULT_SESSION_ID = 0xD1A6
_session_ids = itertools.count(DEFAULT_SESSION_ID)


def next_session_id() -> int:
    return next(_session_ids)

#: The device paces chunks (16 KiB every ~20 ms by default), so a gap of this
#: long between replies means the read is dead, not slow.
STALL_TIMEOUT_S = 10.0


class DiagError(RuntimeError):
    """The device declined or failed a request; ``code`` is DiagStatus.error_code."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class FileEntry:
    name: str
    size: int
    bytes_valid: int
    tier: int  # 1 = flash, 2 = card

    @property
    def is_log(self) -> bool:
        return self.name.endswith(".vdlg")


def _message(session_id: int | None, target_device: str, kind: str,
             read: tuple[str, int, int] | None = None) -> bytes:
    m = diag_pb2.DiagRequest(session_id=next_session_id() if session_id is None else session_id)
    if target_device:
        # On a shared bus leg the target is required, or a hub cannot tell whom
        # the request was meant for; on a dedicated link the link IS the address.
        m.target_device = target_device
    if kind == "read":
        assert read is not None
        m.read.name, m.read.offset, m.read.len = read
    elif kind == "abort":
        m.abort.SetInParent()
    elif kind == "list":
        m.list.SetInParent()
    else:
        raise ValueError(f"unknown DiagRequest kind {kind!r}")
    return m.SerializeToString()


def list_message(*, session_id=None, target_device="") -> bytes:
    return _message(session_id, target_device, "list")


def read_message(name, offset=0, length=0, *, session_id=None,
                 target_device="") -> bytes:
    return _message(session_id, target_device, "read", (name, offset, length))


def abort_message(*, session_id=None, target_device="") -> bytes:
    return _message(session_id, target_device, "abort")


def _replies(recv: Callable[[float], bytes | None], session_id: int,
             timeout: float, clock: Callable[[], float]):
    """Yield the session's replies until the device goes quiet for `timeout`."""
    deadline = clock() + timeout
    while True:
        raw = recv(max(0.0, deadline - clock()))
        if raw is None:
            if clock() >= deadline:
                raise DiagError("timeout",
                                f"no reply from the device for {timeout:.0f} s")
            continue
        r = diag_pb2.DiagReply()
        r.ParseFromString(raw)
        if r.session_id != session_id:
            continue  # someone else's transfer on a shared channel
        deadline = clock() + timeout
        yield r


def list_files(send: Callable[[bytes], None],
               recv: Callable[[float], bytes | None], *,
               session_id: int | None = None, target_device: str = "",
               timeout: float = STALL_TIMEOUT_S,
               clock: Callable[[], float] = time.monotonic) -> list[FileEntry]:
    """Ask for the catalogue. Flash tier first, then card, oldest file first."""
    if session_id is None:
        session_id = next_session_id()
    send(list_message(session_id=session_id, target_device=target_device))
    for r in _replies(recv, session_id, timeout, clock):
        which = r.WhichOneof("body")
        if which == "listing":
            return [FileEntry(f.name, f.size, f.bytes_valid, f.tier)
                    for f in r.listing.files]
        if which == "status" and (r.status.error_code
                                  or r.status.state == diag_pb2.DiagStatus.FAILED):
            raise DiagError(r.status.error_code or "failed", r.status.error_message)
    raise AssertionError("unreachable")


def read_file(send: Callable[[bytes], None],
              recv: Callable[[float], bytes | None], name: str, *,
              offset: int = 0, length: int = 0,
              session_id: int | None = None, target_device: str = "",
              timeout: float = STALL_TIMEOUT_S,
              on_progress: Callable[[int], None] | None = None,
              clock: Callable[[], float] = time.monotonic) -> bytes:
    """Read ``[offset, offset+length)`` of one file; ``length == 0`` = to the end.

    Returns the raw bytes as they sit on the device — for a ``.vdlg`` that is
    header plus ciphertext, exactly what `visio_schema.diag.iter_records`
    opens. Chunks arrive in order; a gap (a chunk whose offset is not the
    next expected byte) is a protocol failure, not something to paper over,
    because a spliced ciphertext decrypts to garbage from the gap on.
    """
    if session_id is None:
        session_id = next_session_id()
    send(read_message(name, offset, length, session_id=session_id,
                      target_device=target_device))
    out = bytearray()
    expect = offset
    for r in _replies(recv, session_id, timeout, clock):
        which = r.WhichOneof("body")
        if which == "chunk":
            c = r.chunk
            if c.name != name or c.offset != expect:
                # Our session, but not our bytes: a device bug, and splicing
                # around it would yield ciphertext that decrypts to garbage.
                try:
                    send(abort_message(session_id=session_id,
                                       target_device=target_device))
                except OSError:
                    pass
                raise DiagError("gap", f"chunk {c.name!r}@{c.offset}, expected {name!r}@{expect}")
            out += c.data
            expect += len(c.data)
            if on_progress is not None:
                on_progress(len(out))
            if c.last:
                return bytes(out)
        elif which == "status":
            s = r.status
            if s.state == diag_pb2.DiagStatus.DONE:
                return bytes(out)
            if s.error_code or s.state == diag_pb2.DiagStatus.FAILED:
                raise DiagError(s.error_code or "failed", s.error_message)
    raise AssertionError("unreachable")
