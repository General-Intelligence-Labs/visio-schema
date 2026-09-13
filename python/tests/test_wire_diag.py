"""`visio_schema.wire.diag` — the host half of the diagnostic-log read path.

Drives the state machine against a scripted device: no socket, no board.
"""
from __future__ import annotations

import pytest

from visio_schema.v1.service.diag import diag_pb2
from visio_schema.wire import diag


class FakeDevice:
    """Answers DiagRequests the way src/diag/diag_service.cpp does."""

    def __init__(self, files: dict[str, bytes], *, chunk=16, name="ego"):
        self.files = files
        self.chunk = chunk
        self.name = name
        self.requests: list[diag_pb2.DiagRequest] = []
        self.outbox: list[bytes] = []

    def send(self, raw: bytes) -> None:
        req = diag_pb2.DiagRequest()
        req.ParseFromString(raw)
        self.requests.append(req)
        if req.target_device and req.target_device != self.name:
            return  # not mine — a hub would relay, a leaf ignores
        which = req.WhichOneof("body")
        if which == "list":
            r = diag_pb2.DiagReply(device=self.name, session_id=req.session_id)
            for i, (n, data) in enumerate(self.files.items()):
                r.listing.files.add(name=n, size=len(data), bytes_valid=len(data),
                                    tier=1 if i == 0 else 2)
            self.outbox.append(r.SerializeToString())
        elif which == "read":
            data = self.files.get(req.read.name)
            if data is None:
                r = diag_pb2.DiagReply(device=self.name, session_id=req.session_id)
                r.status.state = diag_pb2.DiagStatus.FAILED
                r.status.error_code = "no_such_file"
                self.outbox.append(r.SerializeToString())
                return
            end = len(data) if req.read.len == 0 else min(len(data), req.read.offset + req.read.len)
            off = req.read.offset
            while True:
                piece = data[off:min(end, off + self.chunk)]
                r = diag_pb2.DiagReply(device=self.name, session_id=req.session_id)
                r.chunk.name = req.read.name
                r.chunk.offset = off
                r.chunk.data = piece
                off += len(piece)
                r.chunk.last = off >= end
                self.outbox.append(r.SerializeToString())
                if r.chunk.last:
                    break

    def recv(self, timeout: float) -> bytes | None:
        return self.outbox.pop(0) if self.outbox else None


FILES = {"GILABS-TEST0001.3.vdlg": bytes(range(256)) * 3,
         "GILABS-TEST0001.vitals.txt": b"boots=4\nsessions=9\n"}


def test_list_files_returns_the_catalogue():
    dev = FakeDevice(FILES)
    got = diag.list_files(dev.send, dev.recv, target_device="ego")
    assert [f.name for f in got] == list(FILES)
    assert got[0].tier == 1 and got[0].is_log
    assert got[1].tier == 2 and not got[1].is_log
    assert got[0].bytes_valid == 768
    assert dev.requests[0].WhichOneof("body") == "list"
    assert dev.requests[0].session_id >= diag.DEFAULT_SESSION_ID


def test_read_file_reassembles_chunks_in_order():
    dev = FakeDevice(FILES, chunk=100)
    seen = []
    got = diag.read_file(dev.send, dev.recv, "GILABS-TEST0001.3.vdlg",
                         on_progress=seen.append)
    assert got == FILES["GILABS-TEST0001.3.vdlg"]
    assert seen[-1] == 768 and seen == sorted(seen)


def test_read_file_honours_offset_and_length():
    dev = FakeDevice(FILES, chunk=7)
    got = diag.read_file(dev.send, dev.recv, "GILABS-TEST0001.3.vdlg",
                         offset=10, length=20)
    assert got == FILES["GILABS-TEST0001.3.vdlg"][10:30]


class DoneTrailingDevice(FakeDevice):
    """The firmware's real shape: the last chunk is followed by a DONE status."""

    def send(self, raw):
        super().send(raw)
        req = self.requests[-1]
        if req.WhichOneof("body") == "read" and req.read.name in self.files:
            r = diag_pb2.DiagReply(device=self.name, session_id=req.session_id)
            r.status.state = diag_pb2.DiagStatus.DONE
            self.outbox.append(r.SerializeToString())


def test_consecutive_reads_get_their_own_sessions_so_a_stale_done_is_ignored():
    """The host finishes a read at its last chunk; the device's trailing DONE
    is still in flight. Under one shared session id it ended the NEXT read
    with an empty result (seen on hardware: file two of a pull came back
    0 B). Each request now carries its own id, so the stale DONE is not ours."""
    dev = DoneTrailingDevice(FILES, chunk=64)
    first = diag.read_file(dev.send, dev.recv, "GILABS-TEST0001.3.vdlg")
    second = diag.read_file(dev.send, dev.recv, "GILABS-TEST0001.vitals.txt")
    assert first == FILES["GILABS-TEST0001.3.vdlg"]
    assert second == FILES["GILABS-TEST0001.vitals.txt"]
    sids = [r.session_id for r in dev.requests]
    assert len(set(sids)) == len(sids), "every request carries its own session id"


def test_a_failed_listing_raises_the_device_code():
    dev = FakeDevice(FILES)
    r = diag_pb2.DiagReply(session_id=diag.DEFAULT_SESSION_ID)
    r.status.state = diag_pb2.DiagStatus.FAILED
    r.status.error_code = "busy"

    def send(raw):
        req = diag_pb2.DiagRequest()
        req.ParseFromString(raw)
        r.session_id = req.session_id
        dev.outbox.append(r.SerializeToString())

    with pytest.raises(diag.DiagError) as e:
        diag.list_files(send, dev.recv)
    assert e.value.code == "busy"


def test_a_chunk_of_another_file_under_our_session_is_a_gap():
    dev = FakeDevice(FILES, chunk=64)

    def send(raw):
        dev.send(raw)
        req = diag_pb2.DiagRequest()
        req.ParseFromString(raw)
        if req.WhichOneof("body") == "read":
            stray = diag_pb2.DiagReply(session_id=req.session_id)
            stray.chunk.name, stray.chunk.offset, stray.chunk.data = "other.vdlg", 0, b"zz"
            dev.outbox.insert(0, stray.SerializeToString())

    with pytest.raises(diag.DiagError) as e:
        diag.read_file(send, dev.recv, "GILABS-TEST0001.3.vdlg")
    assert e.value.code == "gap"


def test_a_missing_file_raises_the_device_code():
    dev = FakeDevice(FILES)
    with pytest.raises(diag.DiagError) as e:
        diag.read_file(dev.send, dev.recv, "nope.vdlg")
    assert e.value.code == "no_such_file"


def test_another_sessions_replies_are_ignored():
    dev = FakeDevice(FILES, chunk=64)
    stray = diag_pb2.DiagReply(session_id=diag.DEFAULT_SESSION_ID + 1)
    stray.status.state = diag_pb2.DiagStatus.FAILED
    stray.status.error_code = "busy"
    dev.outbox.append(stray.SerializeToString())
    got = diag.read_file(dev.send, dev.recv, "GILABS-TEST0001.vitals.txt")
    assert got == FILES["GILABS-TEST0001.vitals.txt"]


def test_a_gap_aborts_rather_than_splices():
    dev = FakeDevice(FILES, chunk=64)
    diag_send = dev.send

    def lossy_recv(timeout):
        raw = dev.recv(timeout)
        if raw is None:
            return None
        r = diag_pb2.DiagReply()
        r.ParseFromString(raw)
        if r.WhichOneof("body") == "chunk" and r.chunk.offset == 64:
            return dev.recv(timeout)  # drop the second chunk
        return raw

    with pytest.raises(diag.DiagError) as e:
        diag.read_file(diag_send, lossy_recv, "GILABS-TEST0001.3.vdlg")
    assert e.value.code == "gap"
    assert dev.requests[-1].WhichOneof("body") == "abort"


def test_silence_times_out():
    clock = [0.0]

    def recv(timeout):
        clock[0] += timeout or 1.0
        return None

    with pytest.raises(diag.DiagError) as e:
        diag.list_files(lambda raw: None, recv, timeout=3.0, clock=lambda: clock[0])
    assert e.value.code == "timeout"


def test_a_targeted_request_is_ignored_by_another_device():
    dev = FakeDevice(FILES, name="gripper")
    clock = [0.0]

    def recv(timeout):
        r = dev.recv(timeout)
        if r is None:
            clock[0] += 1.0
        return r

    with pytest.raises(diag.DiagError):
        diag.list_files(dev.send, recv, target_device="ego", timeout=2.0,
                        clock=lambda: clock[0])
