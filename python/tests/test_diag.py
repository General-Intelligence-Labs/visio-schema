"""The diagnostic-log reader against docs/protocol/diag_log.md.

Two things are pinned here. The line grammar — every rule in §1 has a case —
and the file round trip: a `.vdlg` built from the same primitives the firmware
uses, read back through the same `open_recording` path, with `bytes_valid`
doing the one job it exists for.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

from visio_schema.diag import (
    SEVERITIES,
    DiagRecord,
    iter_records,
    parse_line,
    resolve_diag_key,
    split_boots,
)
from visio_schema.mcap.crypto import (
    VDLG_SUITE,
    RecordingKeyUnavailable,
    _derive_file_key,
    fingerprint,
)

KEY = bytes(range(32))
NONCE = bytes(range(0x40, 0x4C))

GOOD = "260912 03:14:15.926 +0001234.567 I [COLLECTOR] imu: 474 fps  cam0: 30 fps"
UNSET = "------ --:--:--.--- +0000041.002 W [WIFI] scan found 0 networks"


# ── §1 the line ────────────────────────────────────────────────────────────────


def test_a_stamped_line_parses_every_field():
    r = parse_line(GOOD + "\n")
    assert r.parsed
    assert r.wall == datetime(2026, 9, 12, 3, 14, 15, 926000, tzinfo=timezone.utc)
    assert r.mono_s == pytest.approx(1234.567)
    assert r.severity == "I"
    assert r.tag == "COLLECTOR"
    assert r.message == "imu: 474 fps  cam0: 30 fps"
    assert r.raw == GOOD, "raw is the line as read, without the newline"


def test_the_unset_clock_form_is_none_not_1970():
    r = parse_line(UNSET)
    assert r.parsed
    assert r.wall is None
    assert r.mono_s == pytest.approx(41.002)
    assert r.severity == "W"
    assert r.tag == "WIFI"


def test_dashes_are_both_or_neither():
    # A writer bug, not something to half-trust.
    assert not parse_line("------ 03:14:15.926 +0000001.000 I x").parsed
    assert not parse_line("260912 --:--:--.--- +0000001.000 I x").parsed


def test_fields_split_on_whitespace_not_columns():
    # A monotonic clock past 7 digits widens its field; parsers must not care.
    wide = "260912 03:14:15.926 +12345678.900 E [OTA] no_space"
    r = parse_line(wide)
    assert r.parsed and r.mono_s == pytest.approx(12345678.9) and r.severity == "E"


def test_text_without_a_tag_is_still_a_record():
    r = parse_line("260912 03:14:15.926 +0000001.000 I no tag here k=v")
    assert r.parsed and r.tag is None and r.message == "no tag here k=v"


def test_an_unparseable_line_is_kept_not_dropped():
    for junk in ("[run] /root/umi_embedded missing", "", "260912 garbage", "not a stamp at all"):
        r = parse_line(junk)
        assert r.severity == "?" and not r.parsed
        assert r.raw == junk and r.message == junk
        assert math.isnan(r.mono_s) and r.wall is None


def test_severities_are_the_four_the_contract_names():
    assert SEVERITIES == frozenset("DIWE")


# ── runs ───────────────────────────────────────────────────────────────────────


def _rec(mono: float, tag: str = "X", sev: str = "I") -> DiagRecord:
    return parse_line(f"------ --:--:--.--- +{mono:011.3f} {sev} [{tag}] m")


def test_split_boots_on_the_banner_and_on_a_monotonic_decrease():
    recs = [_rec(1), _rec(2), _rec(3, "BOOT"), _rec(4), _rec(0.5), _rec(9)]
    runs = split_boots(recs)
    assert [len(r) for r in runs] == [2, 2, 2]
    assert runs[1][0].tag == "BOOT"
    assert runs[2][0].mono_s == 0.5


def test_split_boots_keeps_unparsed_lines_with_their_run():
    recs = [_rec(1), parse_line("junk"), _rec(2)]
    runs = split_boots(recs)
    assert len(runs) == 1 and len(runs[0]) == 3


# ── §2 the container, round-tripped ───────────────────────────────────────────


def _vdlg(plain: bytes, bytes_valid: int = 0) -> bytes:
    """A `.vdlg` exactly as the firmware writes one."""
    head = bytearray(32)
    head[0:4] = VDLG_SUITE.magic
    head[4] = head[5] = 1
    head[8:16] = fingerprint(KEY)
    head[16:28] = NONCE
    head[28:32] = bytes_valid.to_bytes(4, "little")
    fk = _derive_file_key(KEY, NONCE, VDLG_SUITE)
    enc = Cipher(algorithms.ChaCha20(fk, (0).to_bytes(4, "little") + NONCE), mode=None).encryptor()
    return bytes(head) + enc.update(plain)


# Monotonic order, as one run writes them: the device boots with no clock
# (UNSET), is given one later, then hits an error.
LINES = [UNSET, GOOD, "260912 03:14:16.000 +0001235.000 E [SD] write failed errno=28"]


def test_an_encrypted_ring_file_reads_back(tmp_path):
    p = tmp_path / "GILABS-7K3M9QP2.3.vdlg"
    p.write_bytes(_vdlg(("\n".join(LINES) + "\n").encode()))
    got = list(iter_records(p, key=KEY))
    assert [r.raw for r in got] == LINES
    assert all(r.parsed for r in got)


def test_bytes_valid_stops_the_reader_at_the_last_flush(tmp_path):
    # The file holds three lines but the writer only ever recorded two as
    # valid: the third is a torn flush, and must not come back as noise.
    two = ("\n".join(LINES[:2]) + "\n").encode()
    three = ("\n".join(LINES) + "\n").encode()
    p = tmp_path / "torn.vdlg"
    p.write_bytes(_vdlg(three, bytes_valid=len(two)))
    got = list(iter_records(p, key=KEY))
    assert [r.raw for r in got] == LINES[:2]


def test_bytes_valid_zero_reads_to_eof(tmp_path):
    p = tmp_path / "eof.vdlg"
    p.write_bytes(_vdlg(("\n".join(LINES) + "\n").encode(), bytes_valid=0))
    assert len(list(iter_records(p, key=KEY))) == 3


def test_a_plaintext_ring_file_needs_no_key(tmp_path):
    # What a dev image with no key staged writes.
    p = tmp_path / "GILABS-7K3M9QP2.0.vdlg"
    p.write_text("\n".join(LINES) + "\n")
    got = list(iter_records(p))
    assert [r.raw for r in got] == LINES


def test_an_encrypted_file_with_no_key_names_every_source(tmp_path, monkeypatch):
    monkeypatch.delenv("VISIO_DIAG_KEY", raising=False)
    monkeypatch.delenv("VISIO_DIAG_KEY_FILE", raising=False)
    monkeypatch.setattr("visio_schema.diag._KEY_FILE", tmp_path / "absent")
    p = tmp_path / "x.vdlg"
    p.write_bytes(_vdlg(b"260912 03:14:15.926 +0000001.000 I x\n"))
    with pytest.raises(RecordingKeyUnavailable, match="VISIO_DIAG_KEY"):
        list(iter_records(p))


def test_key_resolution_order(tmp_path, monkeypatch):
    monkeypatch.delenv("VISIO_DIAG_KEY", raising=False)
    monkeypatch.delenv("VISIO_DIAG_KEY_FILE", raising=False)
    monkeypatch.setattr("visio_schema.diag._KEY_FILE", tmp_path / "absent")
    assert resolve_diag_key() is None
    assert resolve_diag_key(KEY.hex()) == KEY
    monkeypatch.setenv("VISIO_DIAG_KEY", KEY.hex())
    assert resolve_diag_key() == KEY
    monkeypatch.delenv("VISIO_DIAG_KEY")
    kf = tmp_path / "k"
    kf.write_text(KEY.hex() + "\n")
    monkeypatch.setenv("VISIO_DIAG_KEY_FILE", str(kf))
    assert resolve_diag_key() == KEY


# ── the CLI ────────────────────────────────────────────────────────────────────

from visio_schema.diag.cli import main as cli_main  # noqa: E402


def test_cli_open_prints_the_lines(tmp_path, capsys):
    p = tmp_path / "a.vdlg"
    p.write_bytes(_vdlg(("\n".join(LINES) + "\n").encode()))
    assert cli_main(["open", str(p), "--key", KEY.hex()]) == 0
    assert capsys.readouterr().out.splitlines() == LINES


def test_cli_render_summarises_runs_and_lists_warnings(tmp_path, capsys):
    p = tmp_path / "a.vdlg"
    p.write_text("\n".join(LINES) + "\n")  # plaintext: no key needed
    assert cli_main(["render", str(p)]) == 0
    out = capsys.readouterr().out
    assert "3 lines, 1 run(s)" in out
    assert "W=1" in out and "E=1" in out and "I=1" in out
    assert "[SD] write failed errno=28" in out


def test_cli_counters_sorts_and_skips_comments(tmp_path, capsys):
    v = tmp_path / "GILABS-X.vitals.txt"
    v.write_text("# what this is\nsessions=12\nboots=417\n")
    assert cli_main(["counters", str(v)]) == 0
    assert capsys.readouterr().out.splitlines() == ["boots     417", "sessions  12"]


def test_cli_reports_a_missing_key_and_exits_1(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("VISIO_DIAG_KEY", raising=False)
    monkeypatch.delenv("VISIO_DIAG_KEY_FILE", raising=False)
    monkeypatch.setattr("visio_schema.diag._KEY_FILE", tmp_path / "absent")
    p = tmp_path / "a.vdlg"
    p.write_bytes(_vdlg(b"x\n"))
    assert cli_main(["open", str(p)]) == 1
    assert "visio-diag:" in capsys.readouterr().err
