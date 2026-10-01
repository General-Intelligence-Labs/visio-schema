"""wire/recordings against a fake device: a command runner plus a real loopback
TCP sender that behaves like the firmware's (connect, bytes, close)."""
from __future__ import annotations

import socket
import threading

import pytest

from visio_schema.v1.control import command_result_pb2
from visio_schema.wire import recordings as rec

SESSION = "session_00042-1789017895"


class FakeDevice:
    """Serves ListRecordings / OpenRecordingFile / DeleteRecording, and sends
    opened ranges on a loopback socket exactly as the firmware does: one
    connection per open, bytes, close. ``cuts`` makes the next sends stop
    short; ``refuse_sends`` closes every connection without sending."""

    def __init__(self, files: dict[str, bytes], *, sessions: list[str] | None = None,
                 page: int = 2):
        self.files = dict(files)
        self.mtime = {name: 1_789_017_895_000_000_000 + i for i, name in enumerate(files)}
        self.sessions = sessions or [SESSION]
        self.page = page
        self.opens: list = []
        self.cuts: list[int] = []
        self.refuse_sends = False
        self.errors: dict[str, str] = {}
        self._pending = None
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self._server.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                pending, self._pending = self._pending, None
                if pending is None or self.refuse_sends:
                    continue
                data, offset, length = pending
                chunk = data[offset:offset + length]
                if self.cuts:
                    chunk = chunk[:self.cuts.pop(0)]
                try:
                    conn.sendall(chunk)
                    conn.shutdown(socket.SHUT_WR)
                except OSError:  # the host hung up mid-send, as a failing sink does
                    pass

    def run(self, cmd, timeout):
        result = command_result_pb2.CommandResult(command_id=cmd.command_id, ok=True)
        body = cmd.WhichOneof("body")
        if body in self.errors:
            result.ok = False
            result.error_code = self.errors[body]
            return result
        if body == "list_recordings":
            req = cmd.list_recordings
            out = result.recordings
            if req.session_name:
                entry = out.recordings.add(name=req.session_name, file_count=len(self.files))
                for name, data in self.files.items():
                    entry.files.add(name=name, size=len(data), mtime_ns=self.mtime[name],
                                    complete=True)
                return result
            start = int(req.cursor) if req.cursor else 0
            for name in self.sessions[start:start + self.page]:
                out.recordings.add(name=name)
            if start + self.page < len(self.sessions):
                out.next_cursor = str(start + self.page)
            return result
        if body == "open_recording_file":
            req = cmd.open_recording_file
            self.opens.append((req.offset, req.expect_size, req.expect_mtime_ns))
            data = self.files[req.file_name]
            mtime = self.mtime[req.file_name]
            if (req.expect_size or req.expect_mtime_ns) and \
                    (req.expect_size, req.expect_mtime_ns) != (len(data), mtime):
                result.ok = False
                result.error_code = "changed"
                return result
            length = len(data) - req.offset
            self._pending = (data, req.offset, length)
            result.file_open.CopyFrom(command_result_pb2.RecordingFileOpen(
                port=self.port, offset=req.offset, length=length,
                file_size=len(data), mtime_ns=mtime))
            return result
        if body == "delete_recording":
            self.sessions.remove(cmd.delete_recording.session_name)
            return result
        result.ok = False
        result.error_code = "unsupported"
        return result


@pytest.fixture
def device():
    dev = FakeDevice({"ego_0000.mcap": bytes(range(256)) * 4096, "session.json": b"{}"})
    yield dev
    dev.close()


class Collector:
    def __init__(self):
        self.data = bytearray()
        self.offsets: list[int] = []

    def __call__(self, offset: int, chunk: bytes):
        assert offset == len(self.data), "sink offsets must be contiguous"
        self.offsets.append(offset)
        self.data += chunk


# ---- builders -----------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", ".hidden", "a/b", "..", "x" * 64, "nul\0"])
def test_open_refuses_names_the_device_cannot_accept(bad):
    with pytest.raises(ValueError):
        rec.open_recording_file_command(bad, "ego_0000.mcap")
    with pytest.raises(ValueError):
        rec.open_recording_file_command(SESSION, bad)


def test_a_name_at_the_cap_is_accepted():
    rec.open_recording_file_command("s" * rec.MAX_NAME_BYTES, "f" * rec.MAX_NAME_BYTES)


def test_delete_requires_a_target():
    with pytest.raises(ValueError):
        rec.delete_recording_command(SESSION, target_device="")


def test_plain_list_is_the_original_call():
    cmd = rec.list_recordings_command()
    assert cmd.WhichOneof("body") == "list_recordings"
    # field 18, length-delimited, empty: exactly what pre-pull hosts send.
    assert cmd.SerializeToString() == bytes.fromhex("920100")


# ---- listing ------------------------------------------------------------------

def test_list_all_follows_cursors_to_the_last_page():
    dev = FakeDevice({}, sessions=[f"session_{i:05d}-1" for i in range(5)], page=2)
    try:
        names = [e.name for e in rec.list_all_recordings(dev.run)]
    finally:
        dev.close()
    assert names == [f"session_{i:05d}-1" for i in range(5)]


def test_a_cursor_that_does_not_advance_is_a_protocol_error():
    def run(cmd, timeout):
        result = command_result_pb2.CommandResult(ok=True)
        result.recordings.next_cursor = "same"
        return result
    with pytest.raises(rec.RecordingsError) as e:
        rec.list_all_recordings(run)
    assert e.value.code == "protocol"


def test_list_session_files(device):
    entry = rec.list_session_files(device.run, SESSION)
    assert [f.name for f in entry.files] == ["ego_0000.mcap", "session.json"]


# ---- pulling ------------------------------------------------------------------

def test_pull_whole_file_in_one_open(device):
    sink = Collector()
    opened = rec.pull_file(device.run, "127.0.0.1", SESSION, "ego_0000.mcap", sink)
    assert bytes(sink.data) == device.files["ego_0000.mcap"]
    assert opened.file_size == len(device.files["ego_0000.mcap"])
    assert len(device.opens) == 1


def test_an_early_close_resumes_at_the_bytes_received_with_identity_pinned(device):
    device.cuts = [100_000, 300_000]
    sink = Collector()
    rec.pull_file(device.run, "127.0.0.1", SESSION, "ego_0000.mcap", sink)
    assert bytes(sink.data) == device.files["ego_0000.mcap"]
    size, mtime = len(device.files["ego_0000.mcap"]), device.mtime["ego_0000.mcap"]
    assert device.opens == [(0, 0, 0), (100_000, size, mtime), (400_000, size, mtime)]


def test_a_file_that_changes_between_opens_fails_instead_of_splicing(device):
    device.cuts = [50_000]
    sink = Collector()
    real_run = device.run

    def run(cmd, timeout):
        if cmd.WhichOneof("body") == "open_recording_file" and device.opens:
            device.mtime["ego_0000.mcap"] += 1
        return real_run(cmd, timeout)

    with pytest.raises(rec.RecordingsError) as e:
        rec.pull_file(run, "127.0.0.1", SESSION, "ego_0000.mcap", sink)
    assert e.value.code == "changed"
    assert len(sink.data) == 50_000


def test_opens_that_deliver_nothing_give_up(device):
    device.refuse_sends = True
    with pytest.raises(rec.RecordingsError) as e:
        rec.pull_file(device.run, "127.0.0.1", SESSION, "ego_0000.mcap", Collector(),
                      max_opens_without_progress=3)
    assert e.value.code == "no_progress"
    assert len(device.opens) == 3


def test_an_already_complete_file_needs_no_socket(device):
    size = len(device.files["session.json"])
    opened = rec.pull_file(device.run, "127.0.0.1", SESSION, "session.json", Collector(),
                           offset=size)
    assert opened.length == 0


def test_device_refusals_carry_the_device_code(device):
    device.errors["open_recording_file"] = "writing"
    with pytest.raises(rec.RecordingsError) as e:
        rec.pull_file(device.run, "127.0.0.1", SESSION, "ego_0000.mcap", Collector())
    assert e.value.code == "writing"


def test_an_open_answer_for_another_offset_is_a_protocol_error(device):
    def run(cmd, timeout):
        result = device.run(cmd, timeout)
        result.file_open.offset += 1
        return result
    with pytest.raises(rec.RecordingsError) as e:
        rec.open_recording_file(run, SESSION, "ego_0000.mcap")
    assert e.value.code == "protocol"


def test_a_stalled_socket_returns_what_arrived_with_the_reason():
    server = socket.create_server(("127.0.0.1", 0))
    held = []
    threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
    opened = command_result_pb2.RecordingFileOpen(port=server.getsockname()[1], length=10,
                                                 file_size=10)
    got = rec.receive("127.0.0.1", opened, Collector(), stall_timeout=0.2)
    server.close()
    assert got.received == 0
    assert isinstance(got.error, TimeoutError)


def test_sink_failures_propagate(device):
    def sink(offset, chunk):
        raise OSError(28, "No space left on device")
    with pytest.raises(OSError):
        rec.pull_file(device.run, "127.0.0.1", SESSION, "ego_0000.mcap", sink)


def test_delete(device):
    rec.delete_recording(device.run, SESSION, target_device="GILABS-A")
    assert device.sessions == []


def test_pull_quiesce_rules_are_the_spec_section_7_list():
    # The C++ (kPullQuiesceRules) and TS (PULL_QUIESCE_RULES) twins pin the same list.
    assert rec.PULL_QUIESCE_RULES == ("**/camera/*", "**/imu/*/raw", "**/imu/*/quat", "**/audio/*")
