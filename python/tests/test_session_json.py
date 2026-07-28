"""Rebuilding the ``session.json`` sidecar from a recording's embedded capture metadata.

The golden line below is a byte-for-byte copy of a sidecar written by a device before
the firmware stopped emitting them — key order and number formatting included. That
exact layout is the contract: customer pipelines parse these files, so the rebuild has
to reproduce it, not merely produce equivalent JSON.
"""
from __future__ import annotations

import importlib.util
import json
import struct
from pathlib import Path

import pytest

# Loaded by path, not imported: the tool is a standalone single file under tools/, not
# part of the `visio_schema` package — that is what customers download and run, and it
# is what this exercises. Keeping the test here keeps it in `make pytest`.
_TOOL = Path(__file__).resolve().parents[2] / "tools" / "session-json" / "session_json.py"
_spec = importlib.util.spec_from_file_location("session_json_tool", _TOOL)
session_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_json)

SIDECAR_NAME = session_json.SIDECAR_NAME
main = session_json.main
read_capture_metadata = session_json.read_capture_metadata
rebuild_session = session_json.rebuild_session
session_json_text = session_json.session_json_text

GOLDEN = (
    '{"session_name":"session_00003","device_id":"e41cafabfa29d482",'
    '"hostname":"GILABS-ZnR4U9yc","kernel":"Linux 5.10.160 armv7l","app_version":"1.0.2",'
    '"start_time_unix":1782646865.137482,"task":"","location":"","message":"","capturer":"",'
    '"latitude":0.0000000,"longitude":0.0000000,'
    '"client_unix_us":0,"client_utc_offset_min":0,"fps":30}\n'
)

# What the firmware embeds for that session: it omits the empty/zero fields, which the
# sidecar nonetheless always carried.
GOLDEN_META = {
    "session_name": "session_00003",
    "serial": "e41cafabfa29d482",
    "hostname": "GILABS-ZnR4U9yc",
    "kernel": "Linux 5.10.160 armv7l",
    "app_version": "1.0.2",
    "start_time_unix": "1782646865.137482",
    "fps": "30",
}

MAGIC = b"\x89MCAP0\r\n"


def _record(opcode: int, payload: bytes) -> bytes:
    return bytes([opcode]) + struct.pack("<Q", len(payload)) + payload


def _prefixed(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _mcap(metadata: dict[str, str] | None, *, name: str = "visio.capture") -> bytes:
    """A minimal MCAP: magic, Header, an optional Metadata record, magic."""
    out = MAGIC + _record(0x01, _prefixed("") + _prefixed("test"))
    if metadata is not None:
        entries = b"".join(_prefixed(k) + _prefixed(v) for k, v in metadata.items())
        out += _record(0x0C, _prefixed(name) + struct.pack("<I", len(entries)) + entries)
    return out + MAGIC


def _session(tmp_path, name: str, metadata: dict[str, str] | None, part: str = "ego_0000.mcap"):
    session = tmp_path / name
    session.mkdir(parents=True, exist_ok=True)
    (session / part).write_bytes(_mcap(metadata))
    return session


def test_rebuilt_sidecar_is_byte_for_byte_the_one_the_firmware_wrote(tmp_path):
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    ok, message = rebuild_session(session)
    assert ok, message
    assert (session / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


def test_renderer_fills_every_key_the_record_omits():
    """A record carrying nothing still renders the full key set with blank values —
    a consumer indexing a field must not hit a KeyError on a plainly-labelled take."""
    parsed = json.loads(session_json_text({}, session_name="session_00007"))
    assert parsed["session_name"] == "session_00007"   # fallback: the folder name
    assert parsed["task"] == parsed["capturer"] == parsed["device_id"] == ""
    assert parsed["latitude"] == parsed["longitude"] == parsed["start_time_unix"] == 0
    assert parsed["fps"] == parsed["client_unix_us"] == parsed["client_utc_offset_min"] == 0


def test_record_serial_becomes_the_sidecars_device_id():
    """The one field that changed name when the metadata moved into the MCAP."""
    assert json.loads(session_json_text({"serial": "0a620946edfeda76"}))["device_id"] == (
        "0a620946edfeda76")


def test_non_ascii_labels_survive(tmp_path):
    """Task and capturer are routinely Chinese; the sidecar is written UTF-8 (not the
    Windows console codepage) and stays human-readable rather than \\uXXXX escapes."""
    session = _session(tmp_path, "session_00005-1785067199",
                       {**GOLDEN_META, "task": "配菜", "capturer": "周兴波"})
    assert rebuild_session(session)[0]
    raw = (session / SIDECAR_NAME).read_text(encoding="utf-8")
    assert '"task":"配菜"' in raw
    assert json.loads(raw)["capturer"] == "周兴波"


def test_a_quote_in_a_label_stays_valid_json(tmp_path):
    session = _session(tmp_path, "session_00001", {**GOLDEN_META, "message": 'he said "go"'})
    assert rebuild_session(session)[0]
    parsed = json.loads((session / SIDECAR_NAME).read_text(encoding="utf-8"))
    assert parsed["message"] == 'he said "go"'


def test_existing_sidecar_is_kept_unless_forced(tmp_path):
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    (session / SIDECAR_NAME).write_text("hand-edited\n", encoding="utf-8")

    ok, message = rebuild_session(session)
    assert ok and message.startswith("kept")
    assert (session / SIDECAR_NAME).read_text(encoding="utf-8") == "hand-edited\n"

    assert rebuild_session(session, force=True)[0]
    assert (session / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


def test_dry_run_writes_nothing(tmp_path):
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    ok, message = rebuild_session(session, dry_run=True)
    assert ok and "would rebuild" in message
    assert not (session / SIDECAR_NAME).exists()


def test_falls_through_to_a_later_part(tmp_path):
    """Part 0 of a session torn by a power cut can be truncated before its metadata;
    every rolled part re-emits the record, so the rebuild must keep looking."""
    session = _session(tmp_path, "session_00009", None, part="ego_0000.mcap")
    (session / "ego_0001.mcap").write_bytes(_mcap(GOLDEN_META))
    ok, message = rebuild_session(session)
    assert ok and "ego_0001.mcap" in message
    assert (session / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


def test_recording_without_the_record_fails_and_writes_nothing(tmp_path, capsys):
    """Pre-metadata firmware — those sessions still have their original sidecar, so
    inventing a blank one would be worse than saying so."""
    session = _session(tmp_path, "session_00000", None)
    ok, message = rebuild_session(session)
    assert not ok and "no capture metadata" in message
    assert not (session / SIDECAR_NAME).exists()

    assert main([str(session)]) == 1
    assert "no capture metadata" in capsys.readouterr().err


def test_another_tools_metadata_record_is_not_mistaken_for_ours(tmp_path):
    session = _session(tmp_path, "session_00000", {"task": "x"}, part="other.mcap")
    (session / "other.mcap").write_bytes(_mcap({"task": "x"}, name="some.other.tool"))
    assert read_capture_metadata(session / "other.mcap") is None


@pytest.mark.parametrize("keep", [9, 20, 40])
def test_a_truncated_part_is_reported_not_crashed(tmp_path, keep):
    """A garbage length or a record cut mid-write must not raise or hang."""
    session = tmp_path / "session_00000"
    session.mkdir()
    (session / "ego_0000.mcap").write_bytes(_mcap(GOLDEN_META)[:keep])
    ok, message = rebuild_session(session)
    assert not ok and "no capture metadata" in message


def test_walks_a_whole_card(tmp_path, capsys):
    """Point it at the card (or an archive tree) and every session below is rebuilt."""
    _session(tmp_path / "DCIM", "session_00001-1785067100", GOLDEN_META)
    _session(tmp_path / "DCIM", "session_00002-1785067200", GOLDEN_META)
    _session(tmp_path, "session_00003", GOLDEN_META)

    assert main([str(tmp_path)]) == 0
    assert "rebuilt 3, kept 0, failed 0" in capsys.readouterr().out
    assert len(list(tmp_path.rglob(SIDECAR_NAME))) == 3


def test_no_arguments_rebuilds_the_current_folder(tmp_path, monkeypatch):
    """The double-click case: an .exe sitting in the recordings folder, run with no
    arguments at all."""
    _session(tmp_path, "session_00003", GOLDEN_META)
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    assert (tmp_path / "session_00003" / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


def test_a_single_mcap_file_rebuilds_its_own_folder(tmp_path):
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    assert main([str(session / "ego_0000.mcap")]) == 0
    assert (session / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


def test_stdout_prints_utf8_bytes_and_writes_no_file(tmp_path, capsysbinary):
    session = _session(tmp_path, "session_00005", {**GOLDEN_META, "capturer": "周兴波"})
    assert main(["--stdout", str(session)]) == 0
    assert "周兴波".encode() in capsysbinary.readouterr().out
    assert not (session / SIDECAR_NAME).exists()


def test_a_missing_path_is_an_error(tmp_path, capsys):
    assert main([str(tmp_path / "nope")]) == 1
    assert "no such file or folder" in capsys.readouterr().err


def test_an_empty_folder_is_an_error(tmp_path, capsys):
    assert main([str(tmp_path)]) == 1
    assert "no .mcap files found under here" in capsys.readouterr().err
