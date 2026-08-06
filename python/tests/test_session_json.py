"""Rebuilding the ``session.json`` sidecar from a recording's embedded capture metadata.

``LEGACY`` below is a byte-for-byte copy of a sidecar written by a device before the
firmware stopped emitting them — key order and number formatting included. That exact
layout is the contract: customer pipelines parse these files, so the rebuild has to
reproduce it, not merely produce equivalent JSON. Fields added since are spliced onto
that literal rather than edited into it, so the goldens keep holding the bytes a device
really wrote and a key inserted mid-layout fails instead of rewriting the pin.

The other theme here is damaged input. This tool exists to recover recordings off
cards that were yanked or lost power mid-write, so a torn part, a garbage length, or
an unreadable file must cost that *part* and nothing more — never the rest of the
card, and never a confident blank sidecar.
"""
from __future__ import annotations

import importlib.util
import json
import os
import struct
import sys
from pathlib import Path

import pytest

# Loaded by path, not imported: the tool is a standalone single file under tools/, not
# part of the `visio_schema` package — that is what customers download and run, and it
# is what this exercises. Keeping the test here keeps it in `make pytest`.
_TOOL = Path(__file__).resolve().parents[2] / "tools" / "session-json" / "session_json.py"
_spec = importlib.util.spec_from_file_location("session_json_tool", _TOOL)
session_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_json)

FAILED = session_json.FAILED
KEPT = session_json.KEPT
REBUILT = session_json.REBUILT
SIDECAR_NAME = session_json.SIDECAR_NAME
main = session_json.main
read_capture_metadata = session_json.read_capture_metadata
rebuild_session = session_json.rebuild_session
session_json_text = session_json.session_json_text

# The device-written bytes, less the closing brace. Keys added after the layout was
# frozen append to this; see the module docstring.
LEGACY = (
    '{"session_name":"session_00003","device_id":"e41cafabfa29d482",'
    '"hostname":"GILABS-AABBCCDD","kernel":"Linux 5.10.160 armv7l","app_version":"1.0.2",'
    '"start_time_unix":1782646865.137482,"task":"","location":"","message":"","capturer":"",'
    '"latitude":0.0000000,"longitude":0.0000000,'
    '"client_unix_us":0,"client_utc_offset_min":0,"fps":30'
)
FLEET_IDS_BLANK = '"operator_id":"","environment_id":""'
GOLDEN = f"{LEGACY},{FLEET_IDS_BLANK}}}\n"

# What the firmware embeds for that session: it omits the empty/zero fields, which the
# sidecar nonetheless always carried.
GOLDEN_META = {
    "session_name": "session_00003",
    "serial": "e41cafabfa29d482",
    "hostname": "GILABS-AABBCCDD",
    "kernel": "Linux 5.10.160 armv7l",
    "app_version": "1.0.2",
    "start_time_unix": "1782646865.137482",
    "fps": "30",
}

# A second real sidecar, this one with every field the layout then had filled in: a GPS
# fix, a negative UTC offset, a 16-digit microsecond epoch, and free-text labels. LEGACY
# above is an all-defaults take, so on its own it pins almost no number.
LEGACY_GPS = (
    '{"session_name":"session_00012","device_id":"751332c91c5ad2c3",'
    '"hostname":"GILABS-11223344","kernel":"Linux 5.10.160 armv7l","app_version":"1.0.4",'
    '"start_time_unix":1784079342.883779,"task":"test","location":"Test 1",'
    '"message":"10 mbps","capturer":"Shumo",'
    '"latitude":37.7698784,"longitude":-122.4029038,'
    '"client_unix_us":1783818378802000,"client_utc_offset_min":-420,"fps":30'
)
GOLDEN_GPS = f"{LEGACY_GPS},{FLEET_IDS_BLANK}}}\n"

# The same session as the MCAP record carries it. Note lat/lon: the firmware embeds
# them with std::to_string, which is "%f" — SIX decimals — while the sidecar was
# written "%.7f". See test_gps_loses_its_last_digit_at_the_firmware.
GOLDEN_GPS_META = {
    "session_name": "session_00012",
    "serial": "751332c91c5ad2c3",
    "hostname": "GILABS-11223344",
    "kernel": "Linux 5.10.160 armv7l",
    "app_version": "1.0.4",
    "start_time_unix": "1784079342.883779",
    "task": "test",
    "location": "Test 1",
    "message": "10 mbps",
    "capturer": "Shumo",
    "latitude": "37.769878",
    "longitude": "-122.402904",
    "client_unix_us": "1783818378802000",
    "client_utc_offset_min": "-420",
    "fps": "30",
}

MAGIC = b"\x89MCAP0\r\n"

skip_if_root = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="chmod 0 doesn't deny access to root, and Windows ignores the mode bits",
)


def _record(opcode: int, payload: bytes) -> bytes:
    return bytes([opcode]) + struct.pack("<Q", len(payload)) + payload


def _prefixed(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<I", len(raw)) + raw


def _metadata_record(metadata: dict[str, str], name: str = "visio.capture") -> bytes:
    entries = b"".join(_prefixed(k) + _prefixed(v) for k, v in metadata.items())
    return _record(0x0C, _prefixed(name) + struct.pack("<I", len(entries)) + entries)


def _mcap(metadata: dict[str, str] | None, *, name: str = "visio.capture",
          trailer: bytes = b"") -> bytes:
    """A minimal MCAP: magic, Header, an optional Metadata record, magic."""
    out = MAGIC + _record(0x01, _prefixed("") + _prefixed("test"))
    if metadata is not None:
        out += _metadata_record(metadata, name)
    return out + trailer + MAGIC


def _session(tmp_path, name: str, metadata: dict[str, str] | None, part: str = "ego_0000.mcap"):
    session = tmp_path / name
    session.mkdir(parents=True, exist_ok=True)
    (session / part).write_bytes(_mcap(metadata))
    return session


# --- the format contract -------------------------------------------------------


def test_rebuilt_sidecar_is_byte_for_byte_the_one_the_firmware_wrote(tmp_path):
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    status, message = rebuild_session(session)
    assert status == REBUILT, message
    assert (session / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


def test_a_fully_populated_session_round_trips(tmp_path):
    """GPS fix, negative UTC offset, 16-digit epoch, free text — the shapes GOLDEN's
    all-zero take never exercises."""
    session = _session(tmp_path, "session_00012", GOLDEN_GPS_META)
    assert rebuild_session(session)[0] == REBUILT
    rebuilt = json.loads((session / SIDECAR_NAME).read_text(encoding="utf-8"))
    original = json.loads(GOLDEN_GPS)
    assert rebuilt | {"latitude": original["latitude"],       # lossy, see the test below
                      "longitude": original["longitude"]} == original


def test_gps_loses_its_last_digit_at_the_firmware():
    """The sidecar wrote lat/lon at "%.7f"; the record embeds them with std::to_string
    ("%f", six decimals). The 7th digit — about 1 cm — is destroyed before this tool
    ever sees the file, so a GPS session is the one case that is NOT byte-for-byte.
    Pinned so the claim in the README stays honest, and so a firmware fix shows up
    here as a failing test."""
    assert session_json_text(GOLDEN_GPS_META) == (
        GOLDEN_GPS.replace("37.7698784", "37.7698780").replace("-122.4029038", "-122.4029040"))


def test_renderer_fills_every_key_the_record_omits():
    """A record carrying only the identity keys still renders the full key set with
    the blanks the sidecar always carried — at the widths it carried them, so a
    consumer that reads fixed columns or diffs against an old file sees no change."""
    rendered = session_json_text({"session_name": "session_00007"})
    assert '"session_name":"session_00007","device_id":"","hostname":""' in rendered
    assert '"start_time_unix":0.000000' in rendered
    assert '"latitude":0.0000000,"longitude":0.0000000' in rendered
    assert '"client_unix_us":0,"client_utc_offset_min":0,"fps":0' in rendered
    assert rendered.endswith('"operator_id":"","environment_id":""}\n')


def test_fleet_ids_render_after_the_frozen_layout(tmp_path):
    """The set case for the pair GOLDEN pins blank: they render LAST, appended to the
    layout a device wrote, so nothing ahead of them moves when a record carries them."""
    session = _session(tmp_path, "session_00003",
                       {**GOLDEN_META, "operator_id": "op-7", "environment_id": "warehouse-b"})
    assert rebuild_session(session)[0] == REBUILT
    raw = (session / SIDECAR_NAME).read_text(encoding="utf-8")
    assert raw == LEGACY + ',"operator_id":"op-7","environment_id":"warehouse-b"}\n'


def test_record_serial_becomes_the_sidecars_device_id():
    """The one field that changed name when the metadata moved into the MCAP."""
    assert json.loads(session_json_text({"serial": "0a620946edfeda76"}))["device_id"] == (
        "0a620946edfeda76")


def test_microsecond_epoch_keeps_every_digit():
    """client_unix_us is a µs epoch — routed through a float it would round."""
    out = json.loads(session_json_text({"client_unix_us": "1784943350096001"}))
    assert out["client_unix_us"] == 1784943350096001


def test_non_ascii_labels_survive(tmp_path):
    """Task and capturer are routinely Chinese; the sidecar is written UTF-8 (not the
    Windows console codepage) and stays human-readable rather than \\uXXXX escapes."""
    session = _session(tmp_path, "session_00005-1785067199",
                       {**GOLDEN_META, "task": "配菜", "capturer": "周兴波"})
    assert rebuild_session(session)[0] == REBUILT
    raw = (session / SIDECAR_NAME).read_text(encoding="utf-8")
    assert '"task":"配菜"' in raw
    assert json.loads(raw)["capturer"] == "周兴波"


def test_a_quote_in_a_label_is_escaped_the_way_the_firmware_escaped_it(tmp_path):
    """The one place the two escapers could diverge, so the form is the point —
    `\\"`, not the equally-valid `\\u0022`."""
    session = _session(tmp_path, "session_00001", {**GOLDEN_META, "message": 'he said "go"'})
    assert rebuild_session(session)[0] == REBUILT
    raw = (session / SIDECAR_NAME).read_text(encoding="utf-8")
    assert '"message":"he said \\"go\\""' in raw
    assert json.loads(raw)["message"] == 'he said "go"'


# --- damaged input -------------------------------------------------------------


def test_a_garbage_record_length_costs_one_part_not_the_card(tmp_path):
    """A corrupt u64 length is NOT a harmless seek past EOF — it raises ValueError,
    which would escape the caller's OSError guard and kill the whole run mid-card."""
    card = tmp_path / "card"
    torn = card / "session_00001"
    torn.mkdir(parents=True)
    (torn / "ego_0000.mcap").write_bytes(
        MAGIC + _record(0x01, _prefixed("") + _prefixed("test"))[:1]
        + struct.pack("<Q", 0xFFFFFFFFFFFFFFFF) + b"\x00" * 32)
    good = _session(card, "session_00003", GOLDEN_META)

    assert main([str(card)]) == 1                      # the torn session is reported
    assert (good / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")   # the rest still ran


@pytest.mark.parametrize("length", [1 << 63, 1 << 62, (1 << 64) - 1])
def test_no_seekable_length_can_raise(tmp_path, length):
    part = tmp_path / "ego_0000.mcap"
    part.write_bytes(MAGIC + bytes([0x03]) + struct.pack("<Q", length) + b"\x00" * 16)
    assert read_capture_metadata(part) is None


def test_a_record_torn_past_its_identity_keys_falls_through_to_the_next_part(tmp_path):
    """A half-written record parses into a partial dict. Accepting it would produce a
    confident sidecar reading 1970 with a blank device — and would beat the intact
    part sitting right next to it."""
    session = tmp_path / "session_00003"
    session.mkdir()
    whole = _metadata_record(GOLDEN_META)
    (session / "ego_0000.mcap").write_bytes(
        MAGIC + _record(0x01, _prefixed("") + _prefixed("test"))
        + whole[:len(whole) // 3] + MAGIC)
    (session / "ego_0001.mcap").write_bytes(_mcap(GOLDEN_META))

    status, message = rebuild_session(session)
    assert status == REBUILT and "ego_0001.mcap" in message
    assert (session / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


def test_a_partial_record_alone_is_a_failure_not_a_blank_sidecar(tmp_path):
    session = tmp_path / "session_00003"
    session.mkdir()
    whole = _metadata_record(GOLDEN_META)
    (session / "ego_0000.mcap").write_bytes(
        MAGIC + _record(0x01, _prefixed("") + _prefixed("test"))
        + whole[:len(whole) // 3] + MAGIC)
    assert rebuild_session(session)[0] == FAILED
    assert not (session / SIDECAR_NAME).exists()


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "not-a-number"])
def test_a_malformed_number_is_a_damaged_record(tmp_path, value):
    """nan/inf would render JSON no parser can read, and a junk timestamp silently
    becoming 0.000000 would launder corruption into 1970."""
    with pytest.raises(ValueError):
        session_json_text({**GOLDEN_META, "latitude": value})

    session = _session(tmp_path, "session_00003", {**GOLDEN_META, "start_time_unix": value})
    status, message = rebuild_session(session)
    assert status == FAILED and "damaged" in message
    assert not (session / SIDECAR_NAME).exists()


@pytest.mark.parametrize("keep", [9, 20, 40])
def test_a_truncated_part_is_reported_not_crashed(tmp_path, keep):
    session = tmp_path / "session_00000"
    session.mkdir()
    (session / "ego_0000.mcap").write_bytes(_mcap(GOLDEN_META)[:keep])
    status, message = rebuild_session(session)
    assert status == FAILED and "no capture metadata" in message


@skip_if_root
def test_an_unreadable_part_falls_through_to_the_next(tmp_path):
    """A bad sector on part 0 is at least as likely as a truncated one, and the
    rolled parts each re-emit the record."""
    session = tmp_path / "session_00003"
    session.mkdir()
    bad = session / "ego_0000.mcap"
    bad.write_bytes(_mcap(GOLDEN_META))
    bad.chmod(0o000)
    (session / "ego_0001.mcap").write_bytes(_mcap(GOLDEN_META))

    status, message = rebuild_session(session)
    assert status == REBUILT and "ego_0001.mcap" in message


@skip_if_root
def test_an_unreadable_part_is_named_when_nothing_else_works(tmp_path, capsys):
    session = tmp_path / "session_00003"
    session.mkdir()
    bad = session / "ego_0000.mcap"
    bad.write_bytes(_mcap(GOLDEN_META))
    bad.chmod(0o000)

    assert main([str(session)]) == 1
    assert "ego_0000.mcap" in capsys.readouterr().err


@skip_if_root
def test_an_unsearchable_folder_is_reported_not_skipped(tmp_path, capsys):
    """os.walk drops unreadable directories silently by default — the tool would
    report success while never seeing a whole branch of the card."""
    locked = tmp_path / "locked"
    _session(locked, "session_00003", GOLDEN_META)
    locked.chmod(0o000)
    try:
        assert main([str(tmp_path)]) == 1
        assert "cannot search this folder" in capsys.readouterr().err
    finally:
        locked.chmod(0o755)


def test_metadata_after_the_data_section_is_not_hunted(tmp_path):
    """Stopping at the first data record is what keeps a miss on an old 1 GB
    recording cheap. The recorder always writes the record at index 1, so this only
    gives up on files no Visio device produces."""
    session = tmp_path / "session_00000"
    session.mkdir()
    (session / "ego_0000.mcap").write_bytes(
        _mcap(None, trailer=_record(0x06, b"\x00" * 8) + _metadata_record(GOLDEN_META)))
    assert rebuild_session(session)[0] == FAILED


def test_another_tools_metadata_record_is_not_mistaken_for_ours(tmp_path):
    session = tmp_path / "session_00000"
    session.mkdir()
    (session / "other.mcap").write_bytes(_mcap({"task": "x"}, name="some.other.tool"))
    assert read_capture_metadata(session / "other.mcap") is None


def test_the_walk_continues_past_a_foreign_metadata_record(tmp_path):
    """Ours needn't be the first Metadata record in the file — keep walking, as the
    device's own reader does."""
    part = tmp_path / "ego_0000.mcap"
    part.write_bytes(MAGIC + _record(0x01, _prefixed("") + _prefixed("test"))
                     + _metadata_record({"note": "x"}, "some.other.tool")
                     + _metadata_record(GOLDEN_META) + MAGIC)
    assert read_capture_metadata(part) == GOLDEN_META


def test_a_file_that_is_not_an_mcap_at_all(tmp_path):
    """A half-finished download or a renamed file — not a crash, just no metadata."""
    part = tmp_path / "ego_0000.mcap"
    part.write_bytes(b"<!DOCTYPE html>\n" + b"\x00" * 64)
    assert read_capture_metadata(part) is None


def test_an_appledouble_twin_does_not_shadow_the_real_part(tmp_path):
    """A card copied via macOS carries ._ego_0000.mcap siblings, and they sort first."""
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    (session / "._ego_0000.mcap").write_bytes(b"\x00\x05\x16\x07" * 4)
    status, message = rebuild_session(session)
    assert status == REBUILT and message.endswith("ego_0000.mcap")
    assert (session / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


@skip_if_root
def test_a_read_only_card_is_reported_not_crashed(tmp_path, capsys):
    """A worn card often remounts read-only — the one failure that isn't the file's
    fault, and the user can act on it."""
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    session.chmod(0o555)
    try:
        assert main([str(session)]) == 1
        assert f"cannot write {SIDECAR_NAME}" in capsys.readouterr().err
    finally:
        session.chmod(0o755)


def test_an_uploaded_tombstone_is_not_a_part(tmp_path):
    """The uploader parks `<part>.mcap.uploaded` next to the data it already sent."""
    session = tmp_path / "session_00003"
    session.mkdir()
    (session / "ego_0000.mcap.uploaded").write_bytes(_mcap(GOLDEN_META))
    assert session_json.mcap_parts(session) == []


def test_an_uppercase_extension_is_still_a_part(tmp_path):
    session = _session(tmp_path, "session_00003", GOLDEN_META, part="EGO_0000.MCAP")
    assert rebuild_session(session)[0] == REBUILT
    assert main([str(tmp_path)]) == 0


# --- the CLI -------------------------------------------------------------------


def test_existing_sidecar_is_kept_unless_forced(tmp_path):
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    (session / SIDECAR_NAME).write_text("hand-edited\n", encoding="utf-8")

    assert rebuild_session(session)[0] == KEPT
    assert (session / SIDECAR_NAME).read_text(encoding="utf-8") == "hand-edited\n"

    assert rebuild_session(session, force=True)[0] == REBUILT
    assert (session / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


def test_force_over_an_unrebuildable_session_keeps_it_and_is_not_a_failure(tmp_path):
    """--force on a mixed archive shouldn't turn every legacy session that still has
    its original sidecar into a failure — nothing was lost."""
    session = _session(tmp_path, "session_00000", None)
    (session / SIDECAR_NAME).write_text("original\n", encoding="utf-8")

    status, message = rebuild_session(session, force=True)
    assert status == KEPT and "no capture metadata" in message
    assert (session / SIDECAR_NAME).read_text(encoding="utf-8") == "original\n"
    assert main(["--force", str(session)]) == 0


def test_dry_run_writes_nothing(tmp_path):
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    status, message = rebuild_session(session, dry_run=True)
    assert status == REBUILT and "would rebuild" in message
    assert not (session / SIDECAR_NAME).exists()


def test_recording_without_the_record_fails_and_writes_nothing(tmp_path, capsys):
    """Pre-metadata firmware — those sessions still have their original sidecar, so
    inventing a blank one would be worse than saying so."""
    session = _session(tmp_path, "session_00000", None)
    status, message = rebuild_session(session)
    assert status == FAILED and "no capture metadata" in message
    assert not (session / SIDECAR_NAME).exists()

    assert main([str(session)]) == 1
    assert "no capture metadata" in capsys.readouterr().err


def test_walks_a_whole_card(tmp_path, capsys):
    """Point it at the card (or an archive tree) and every session below is rebuilt."""
    _session(tmp_path / "DCIM", "session_00001-1785067100", GOLDEN_META)
    _session(tmp_path / "DCIM", "session_00002-1785067200", GOLDEN_META)
    _session(tmp_path, "session_00003", GOLDEN_META)

    assert main([str(tmp_path)]) == 0
    assert "rebuilt 3, kept 0, failed 0" in capsys.readouterr().out
    assert len(list(tmp_path.rglob(SIDECAR_NAME))) == 3


def test_a_mixed_card_counts_each_outcome(tmp_path, capsys):
    _session(tmp_path, "session_00001", GOLDEN_META)
    kept = _session(tmp_path, "session_00002", GOLDEN_META)
    (kept / SIDECAR_NAME).write_text("original\n", encoding="utf-8")
    _session(tmp_path, "session_00003", None)

    assert main([str(tmp_path)]) == 1
    assert "rebuilt 1, kept 1, failed 1" in capsys.readouterr().out


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


def test_any_dropped_file_identifies_its_session(tmp_path):
    """Drag-and-drop is the headline UX, so a dropped sidecar — or any stray file —
    must be answered, not met with a traceback."""
    session = _session(tmp_path, "session_00003", GOLDEN_META)
    (session / "notes.txt").write_text("hello\n", encoding="utf-8")
    assert main([str(session / "notes.txt")]) == 0
    assert (session / SIDECAR_NAME).read_bytes() == GOLDEN.encode("utf-8")


def test_a_dropped_file_from_a_folder_with_no_recordings_is_reported(tmp_path, capsys):
    (tmp_path / "notes.txt").write_text("hello\n", encoding="utf-8")
    assert main([str(tmp_path / "notes.txt")]) == 1
    assert "no capture metadata" in capsys.readouterr().err


def test_stdout_prints_utf8_bytes_and_writes_no_file(tmp_path, capsysbinary):
    session = _session(tmp_path, "session_00005", {**GOLDEN_META, "capturer": "周兴波"})
    assert main(["--stdout", str(session)]) == 0
    out = capsysbinary.readouterr().out
    assert "周兴波".encode() in out
    assert json.loads(out.decode("utf-8"))["session_name"] == "session_00003"
    assert not (session / SIDECAR_NAME).exists()


@skip_if_root
def test_stdout_reports_an_unreadable_part_instead_of_crashing(tmp_path, capsys):
    """The --stdout path used to skip the guard the write path had."""
    session = tmp_path / "session_00003"
    session.mkdir()
    bad = session / "ego_0000.mcap"
    bad.write_bytes(_mcap(GOLDEN_META))
    bad.chmod(0o000)

    assert main(["--stdout", str(session)]) == 1
    assert "ego_0000.mcap" in capsys.readouterr().err


def test_a_missing_path_is_an_error(tmp_path, capsys):
    assert main([str(tmp_path / "nope")]) == 1
    assert "no such file or folder" in capsys.readouterr().err


def test_an_empty_folder_is_an_error(tmp_path, capsys):
    assert main([str(tmp_path)]) == 1
    assert "contains no .mcap files" in capsys.readouterr().err
