"""The device diagnostic log: the reference line parser and the file reader.

``docs/protocol/diag_log.md`` is the contract; this module is its reference
implementation. Firmware produces the lines, this reads them, and the two are
pinned together by the tests here and the golden crypto vectors.

Deliberately NOT part of the ``visio_schema`` facade: that surface is pinned by
``tests/test_public_api.py`` and widening it is a versioned change. Import it as
``visio_schema.diag``.
"""
from __future__ import annotations

import io
import math
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from visio_schema.mcap.crypto import (
    HEADER_BYTES,
    VDLG_SUITE,
    RecordingKeyUnavailable,
    is_vrec,
    open_recording,
    parse_key,
)

__all__ = [
    "SEVERITIES",
    "UNSET_DATE",
    "UNSET_TIME",
    "DiagRecord",
    "iter_records",
    "parse_line",
    "resolve_diag_key",
    "split_boots",
]

#: The four severities a writer may emit. Anything else is reserved.
SEVERITIES = frozenset("DIWE")
#: The wall-clock fields when the device was never given a clock this boot.
UNSET_DATE = "------"
UNSET_TIME = "--:--:--.---"

# Whitespace-separated, per the contract: fixed widths are for eyes, not parsers.
_STAMP = re.compile(
    r"^(?P<date>\d{6}|-{6}) "
    r"(?P<time>\d{2}:\d{2}:\d{2}\.\d{3}|-{2}:-{2}:-{2}\.-{3}) "
    r"\+(?P<mono>\d+\.\d{3}) "
    r"(?P<sev>[A-Z])"
    r"(?: (?P<text>.*))?$"
)
_TAG = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<msg>.*)$")

_ENV_KEY = "VISIO_DIAG_KEY"
_ENV_KEY_FILE = "VISIO_DIAG_KEY_FILE"
_KEY_FILE = Path("~/.config/visio/diag-key")


@dataclass(frozen=True)
class DiagRecord:
    """One parsed line. ``raw`` is always the line exactly as read."""

    #: UTC wall clock, or None when the device had no clock (the dashed form).
    wall: datetime | None
    #: Seconds since boot. NaN for a line that did not parse.
    mono_s: float
    #: One of ``SEVERITIES``, or ``"?"`` for a line that did not parse.
    severity: str
    #: The bracketed subsystem tag, without brackets; None if the text had none.
    tag: str | None
    #: The text after the tag (or the whole text if there was no tag).
    message: str
    raw: str

    @property
    def parsed(self) -> bool:
        return self.severity != "?"


def _unparsed(raw: str) -> DiagRecord:
    return DiagRecord(None, math.nan, "?", None, raw, raw)


def parse_line(line: str) -> DiagRecord:
    """Parse one line per §1 of the contract.

    Never raises and never drops: a line that does not fit comes back with
    ``severity == "?"`` and its text in ``message``/``raw``, so a caller can
    count them and a reader is never handed a log that quietly lost lines.
    """
    raw = line.rstrip("\r\n")
    m = _STAMP.match(raw)
    if not m:
        return _unparsed(raw)
    wall: datetime | None
    if m["date"] == UNSET_DATE:
        # Both-or-neither is the rule; a mixed stamp is a writer bug and is
        # surfaced as unparsed rather than half-trusted.
        if m["time"] != UNSET_TIME:
            return _unparsed(raw)
        wall = None
    else:
        if m["time"] == UNSET_TIME:
            return _unparsed(raw)
        try:
            wall = datetime.strptime(
                f"20{m['date']} {m['time']}", "%Y%m%d %H:%M:%S.%f"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return _unparsed(raw)
    text = m["text"] or ""
    tag = None
    message = text
    t = _TAG.match(text)
    if t:
        tag, message = t["tag"], t["msg"]
    return DiagRecord(wall, float(m["mono"]), m["sev"], tag, message, raw)


def split_boots(records: Iterable[DiagRecord]) -> list[list[DiagRecord]]:
    """Group records by run: a new group starts on a ``[BOOT]`` line or
    whenever the monotonic clock goes backwards, whichever comes first.

    Unparsed records stay with whatever run they were read in.
    """
    runs: list[list[DiagRecord]] = []
    last_mono = -math.inf
    for r in records:
        starts_run = r.tag == "BOOT" or (
            r.parsed and r.mono_s < last_mono
        )
        if starts_run or not runs:
            runs.append([])
        runs[-1].append(r)
        if r.parsed:
            last_mono = r.mono_s
    return runs


def resolve_diag_key(explicit: bytes | str | None = None) -> bytes | None:
    """Find the fleet diagnostic key: the argument, then ``$VISIO_DIAG_KEY``
    (hex), then ``$VISIO_DIAG_KEY_FILE``, then ``~/.config/visio/diag-key``.

    Returns None when nothing was found — a plaintext log needs no key, so
    "none" is only an error once an encrypted file is actually opened.
    """
    if explicit is not None:
        # Raw bytes come from a caller that already resolved the key — the CLI
        # hands iter_records what it resolved — and must not be re-parsed as hex.
        if isinstance(explicit, (bytes, bytearray)):
            if len(explicit) != 32:
                raise ValueError(
                    f"diagnostic key must be 32 raw bytes, got {len(explicit)}"
                )
            return bytes(explicit)
        return parse_key(explicit)
    hexkey = os.environ.get(_ENV_KEY)
    if hexkey:
        return parse_key(hexkey.strip())
    for candidate in (os.environ.get(_ENV_KEY_FILE), _KEY_FILE):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return parse_key(path.read_text().strip())
    return None


def iter_records(
    path: str | os.PathLike[str], key: bytes | str | None = None
) -> Iterator[DiagRecord]:
    """Yield one record per line of a ``.vdlg`` file, decrypting if needed.

    A file with no ``VDLG`` magic is read as plaintext — what a dev image
    with no key staged writes. An encrypted file with no key available raises
    ``RecordingKeyUnavailable`` naming every source tried.
    """
    p = Path(path)
    with open(p, "rb") as raw:
        head = raw.read(HEADER_BYTES)
    if is_vrec(head, VDLG_SUITE):
        resolved = resolve_diag_key(key)
        if resolved is None:
            raise RecordingKeyUnavailable(
                f"{p.name} is encrypted and no diagnostic key was found: pass "
                f"key=, or set ${_ENV_KEY} / ${_ENV_KEY_FILE}, or put the hex "
                f"key in {_KEY_FILE}"
            )
        f = open_recording(p, resolved, suite=VDLG_SUITE)
    else:
        f = open(p, "rb")
    # Streamed, not slurped: a card-tier file is 32 MiB by default.
    buffered = io.BufferedReader(f) if isinstance(f, io.RawIOBase) else f
    with io.TextIOWrapper(buffered, encoding="utf-8", errors="replace") as text:
        for line in text:
            line = line.rstrip("\r\n")
            if line:
                yield parse_line(line)
