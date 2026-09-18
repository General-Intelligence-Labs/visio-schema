"""Pull recorded sessions off a device: list, open, receive, delete. Owns no bus connection.

Control rides the existing Command / CommandResult pair (``ListRecordings``,
``OpenRecordingFile``, ``DeleteRecording``). The file bytes do NOT ride the bus:
an ``OpenRecordingFile`` answer names a TCP port on the device's USB-NCM
address, and the host connects, reads until the device closes, and never
writes. Exactly ``length`` bytes means the range arrived whole; fewer means
open again at the bytes received. TCP carries order, delivery and flow control,
so nothing here re-implements them. The contract is
docs/protocol/recordings_pull.md.

Commands are transport-agnostic: every call takes ``run(command, timeout) ->
CommandResult``. The caller stamps ``command_id``, sends on COMMAND and returns
the matching result from ``/<device>/command_result``. `receive` opens its own
socket, because the byte path is a plain socket by design.
"""
from __future__ import annotations

import socket
from collections.abc import Callable
from typing import NamedTuple

from visio_schema.v1.control import command_pb2, command_result_pb2

__all__ = [
    "COMMAND_TIMEOUT_S",
    "DEFAULT_PORT",
    "MAX_NAME_BYTES",
    "MAX_OPENS_WITHOUT_PROGRESS",
    "PULL_QUIESCE_RULES",
    "STALL_TIMEOUT_S",
    "Received",
    "RecordingsError",
    "delete_recording",
    "delete_recording_command",
    "list_all_recordings",
    "list_recordings",
    "list_recordings_command",
    "list_session_files",
    "open_recording_file",
    "open_recording_file_command",
    "pull_file",
    "receive",
]

#: Where the device's file sender listens, on its USB-NCM address only. The
#: open reports the port; this is the value current firmware uses.
DEFAULT_PORT = 50002

#: How long to wait for one CommandResult.
COMMAND_TIMEOUT_S = 8.0

#: No byte for this long on the data socket means the transfer is dead; the
#: bytes that did arrive are kept and the next open resumes after them.
STALL_TIMEOUT_S = 10.0

#: Opens in a row that deliver nothing before a pull gives up.
MAX_OPENS_WITHOUT_PROGRESS = 5

#: What a host drops on its own bus link while it pulls (SetStreamPolicy rules,
#: section 7): the bulk streams — video, IMU samples, audio. Restore on every
#: exit. The C++ and TS twins spell the same tuple.
PULL_QUIESCE_RULES = ("**/camera/*", "**/imu/*/raw", "**/imu/*/quat", "**/audio/*")

#: Session names, file names and cursors are all capped at 63 UTF-8 bytes
#: (nanopb.options max_size:64). An oversized string does not truncate on the
#: device, it fails the whole decode and the device answers nothing, so the cap
#: is enforced here before anything is sent.
MAX_NAME_BYTES = 63

_RECV_BUFFER_BYTES = 1 << 20

Runner = Callable[[command_pb2.Command, float], command_result_pb2.CommandResult]
Sink = Callable[[int, bytes], None]


class RecordingsError(RuntimeError):
    """The device refused a command (``code`` is CommandResult.error_code), or a
    pull could not finish (``no_progress``, ``protocol``)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class Received(NamedTuple):
    """What one `receive` delivered. ``error`` is why it stopped short, if it did."""

    received: int
    error: OSError | None


def _check_name(what: str, name: str, *, required: bool = True) -> None:
    if not name:
        if required:
            raise ValueError(f"{what} is empty")
        return
    raw = name.encode()
    if len(raw) > MAX_NAME_BYTES:
        raise ValueError(f"{what} is {len(raw)} bytes; the limit is {MAX_NAME_BYTES}")
    # A leading dot also rejects "." and ".." — the C++ and TS twins say so too.
    if "/" in name or "\0" in name or name.startswith("."):
        raise ValueError(f"{what} {name!r} is not a plain name")


def list_recordings_command(*, limit: int = 0, cursor: str = "", session_name: str = "",
                            target_device: str = "") -> command_pb2.Command:
    """A ListRecordings. No cursor and no session = the original newest-``limit`` call."""
    if len(cursor.encode()) > MAX_NAME_BYTES:
        raise ValueError(f"cursor is {len(cursor.encode())} bytes; the limit is {MAX_NAME_BYTES}")
    _check_name("session_name", session_name, required=False)
    cmd = command_pb2.Command(target_device=target_device)
    body = cmd.list_recordings
    body.SetInParent()
    body.limit = limit
    body.cursor = cursor
    body.session_name = session_name
    return cmd


def open_recording_file_command(session_name: str, file_name: str, *, offset: int = 0,
                                expect_size: int = 0, expect_mtime_ns: int = 0,
                                target_device: str = "") -> command_pb2.Command:
    """An OpenRecordingFile. ``expect_*`` both 0 = unchecked."""
    _check_name("session_name", session_name)
    _check_name("file_name", file_name)
    cmd = command_pb2.Command(target_device=target_device)
    body = cmd.open_recording_file
    body.session_name = session_name
    body.file_name = file_name
    body.offset = offset
    body.expect_size = expect_size
    body.expect_mtime_ns = expect_mtime_ns
    return cmd


def delete_recording_command(session_name: str, *, target_device: str) -> command_pb2.Command:
    """A DeleteRecording. The device refuses a broadcast delete, so a target is required."""
    _check_name("session_name", session_name)
    if not target_device:
        raise ValueError("delete_recording needs target_device: a broadcast delete is refused")
    cmd = command_pb2.Command(target_device=target_device)
    cmd.delete_recording.session_name = session_name
    return cmd


def _run(run: Runner, cmd: command_pb2.Command, timeout: float) -> command_result_pb2.CommandResult:
    result = run(cmd, timeout)
    if not result.ok:
        raise RecordingsError(result.error_code or "failed", result.error_message)
    return result


def _payload(result: command_result_pb2.CommandResult, want: str, what: str):
    got = result.WhichOneof("payload")
    if got != want:
        raise RecordingsError("protocol", f"{what} answered payload {got!r}, expected {want!r}")
    return getattr(result, want)


def list_recordings(run: Runner, *, limit: int = 0, cursor: str = "", target_device: str = "",
                    timeout: float = COMMAND_TIMEOUT_S) -> command_result_pb2.RecordingsList:
    """One page of sessions, newest first, without files."""
    result = _run(run, list_recordings_command(limit=limit, cursor=cursor,
                                               target_device=target_device), timeout)
    return _payload(result, "recordings", "ListRecordings")


def list_all_recordings(
    run: Runner, *, page_limit: int = 0, target_device: str = "",
    timeout: float = COMMAND_TIMEOUT_S,
) -> list[command_result_pb2.RecordingEntry]:
    """Every session, following cursors to the last page."""
    entries: list[command_result_pb2.RecordingEntry] = []
    cursor = ""
    seen: set[str] = set()
    while True:
        page = list_recordings(run, limit=page_limit, cursor=cursor,
                               target_device=target_device, timeout=timeout)
        entries.extend(page.recordings)
        if not page.next_cursor:
            return entries
        if page.next_cursor in seen:
            raise RecordingsError("protocol", f"cursor {page.next_cursor!r} did not advance")
        seen.add(page.next_cursor)
        cursor = page.next_cursor


def list_session_files(run: Runner, session_name: str, *, target_device: str = "",
                       timeout: float = COMMAND_TIMEOUT_S) -> command_result_pb2.RecordingEntry:
    """One session with its files (``RecordingEntry.files``)."""
    result = _run(run, list_recordings_command(session_name=session_name,
                                               target_device=target_device), timeout)
    page = _payload(result, "recordings", "ListRecordings")
    if len(page.recordings) != 1 or page.recordings[0].name != session_name:
        raise RecordingsError("protocol", f"asked for {session_name!r}, got "
                              f"{[e.name for e in page.recordings]!r}")
    return page.recordings[0]


def open_recording_file(run: Runner, session_name: str, file_name: str, *, offset: int = 0,
                        expect_size: int = 0, expect_mtime_ns: int = 0, target_device: str = "",
                        timeout: float = COMMAND_TIMEOUT_S) -> command_result_pb2.RecordingFileOpen:
    """Open one file for reading from ``offset``; the answer says where and how much to read."""
    cmd = open_recording_file_command(session_name, file_name, offset=offset,
                                      expect_size=expect_size, expect_mtime_ns=expect_mtime_ns,
                                      target_device=target_device)
    opened = _payload(_run(run, cmd, timeout), "file_open", "OpenRecordingFile")
    if opened.offset != offset or opened.offset + opened.length != opened.file_size:
        raise RecordingsError("protocol", f"open for offset {offset} answered offset "
                              f"{opened.offset}, length {opened.length}, size {opened.file_size}")
    return opened


def receive(address: str, opened: command_result_pb2.RecordingFileOpen, sink: Sink, *,
            stall_timeout: float = STALL_TIMEOUT_S) -> Received:
    """Read one opened range from the device into ``sink(offset, data)``.

    Connects, reads until the device closes or ``opened.length`` bytes have
    arrived, and never writes. A short read is not an error here — a refused
    connection, a reset, a stall and an early close all return what arrived,
    with the reason in ``error``; the caller opens again at the bytes received.
    Exceptions from ``sink`` propagate: a full disk is not a reason to retry.

    This reads only the data socket. The caller keeps its bus link read for the
    duration (section 4): the device aborts a bus connection left unread 10 s.
    """
    try:
        sock = socket.create_connection((address, opened.port), timeout=stall_timeout)
    except OSError as err:
        return Received(0, err)
    received = 0
    buf = bytearray(_RECV_BUFFER_BYTES)
    view = memoryview(buf)
    with sock:
        sock.settimeout(stall_timeout)
        while received < opened.length:
            # Only the socket is guarded: an error from the sink is the caller's
            # (a full disk must stop the pull, not look like a dropped link).
            try:
                n = sock.recv_into(view, min(len(buf), opened.length - received))
            except OSError as err:  # includes socket.timeout
                return Received(received, err)
            if n == 0:
                break
            sink(opened.offset + received, bytes(view[:n]))
            received += n
    return Received(received, None)


def pull_file(run: Runner, address: str, session_name: str, file_name: str, sink: Sink, *,
              offset: int = 0, expect_size: int = 0, expect_mtime_ns: int = 0,
              target_device: str = "", on_progress: Callable[[int, int], None] | None = None,
              stall_timeout: float = STALL_TIMEOUT_S, timeout: float = COMMAND_TIMEOUT_S,
              max_opens_without_progress: int = MAX_OPENS_WITHOUT_PROGRESS,
              ) -> command_result_pb2.RecordingFileOpen:
    """Pull ``file_name`` from ``offset`` to its end into ``sink(offset, data)``.

    Opens, receives, and on a short read opens again at the bytes received,
    pinning the file's size and mtime from the first open so a file that
    changes between attempts fails with "changed" instead of being spliced.
    Returns the last open (its ``file_size`` and ``mtime_ns`` identify the
    file). Device refusals raise RecordingsError with the device's code.
    """
    idle_opens = 0
    last_error: OSError | None = None
    while True:
        opened = open_recording_file(run, session_name, file_name, offset=offset,
                                     expect_size=expect_size, expect_mtime_ns=expect_mtime_ns,
                                     target_device=target_device, timeout=timeout)
        expect_size, expect_mtime_ns = opened.file_size, opened.mtime_ns
        if opened.length == 0:
            return opened
        got = receive(address, opened, sink, stall_timeout=stall_timeout)
        offset += got.received
        if on_progress is not None:
            on_progress(offset, opened.file_size)
        if offset == opened.file_size:
            return opened
        if got.received:
            idle_opens = 0
        else:
            idle_opens += 1
            last_error = got.error
            if idle_opens >= max_opens_without_progress:
                raise RecordingsError("no_progress", f"{idle_opens} opens delivered no bytes "
                                      f"at offset {offset} (last: {last_error!r})")


def delete_recording(run: Runner, session_name: str, *, target_device: str,
                     timeout: float = COMMAND_TIMEOUT_S) -> None:
    """Delete one whole session. Refusals raise RecordingsError with the device's code."""
    _run(run, delete_recording_command(session_name, target_device=target_device), timeout)
