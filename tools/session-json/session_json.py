"""visio-session-json — rebuild a recording's ``session.json`` sidecar from its MCAP.

Devices used to drop a small ``session.json`` next to the ``.mcap`` parts of every
recording session. The firmware now writes that capture metadata *inside* the MCAP
instead (the ``visio.capture`` Metadata record), so a file copied off the card is
self-contained and the sidecar is gone. This rebuilds it — same keys, same order,
same number formatting — for pipelines that still read it::

    visio-session-json D:\\recordings     # a card, a session folder, or one .mcap
    python session_json.py ~/recordings   # same, with any stock Python 3.10+

On Windows the frozen build is also drag-and-drop and double-click, and it holds its
console window open so the report can be read.

One file, stdlib only, on purpose — it reads the few MCAP record bytes it needs
rather than importing the ``mcap`` library, so it can be mailed to a customer and run
as-is. Sidecars are always written UTF-8 with ``\\n`` endings, whatever the console
codepage: a task or capturer name is routinely non-ASCII.

Only sessions recorded by firmware that embeds the record can be rebuilt; older
recordings predate it — and they still have their original sidecar.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

__all__ = ["FAILED", "KEPT", "REBUILT", "SIDECAR_NAME", "find_sessions", "main",
           "mcap_parts", "read_capture_metadata", "rebuild_session", "session_json_text",
           "session_text"]

SIDECAR_NAME = "session.json"

#: What became of one session — the caller counts these rather than reading the
#: human-readable message that travels beside them.
REBUILT, KEPT, FAILED = "rebuilt", "kept", "failed"

# MCAP framing (mcap.dev spec): file = MAGIC, records, MAGIC; each top-level record
# is [opcode u8][length u64le][payload]. The recorder emits the capture Metadata
# record right after the Header — record 1 — and re-emits it in every rolled part.
_MAGIC = b"\x89MCAP0\r\n"
_OP_METADATA = 0x0C
# Message, Chunk, DataEnd. Reaching any of them means the data section has begun and
# the record isn't coming: stop. Without this, a miss on an old-firmware recording
# crawls ~200 records scattered across the whole file — 1.6 MB read off an SD card,
# per part, to learn nothing.
_OP_DATA_BEGINS = frozenset({0x05, 0x06, 0x0F})
_CAPTURE_RECORD = "visio.capture"
# Same budget and caps as the device's own reader: two readers of one record
# shouldn't disagree about what a
# valid file is. The real record is ~200 bytes and sits at index 1.
_MAX_RECORDS = 64
_MIN_PAYLOAD = 8
_MAX_PAYLOAD = 256 * 1024

# Every firmware that writes the record writes these three unconditionally, and the
# map is serialized in key order, so a record torn mid-write loses them first. Their
# absence means "this copy is damaged", not "the device didn't record it" — which is
# what lets a torn part 0 fall through to an intact part 1 instead of winning with a
# confident, mostly-blank sidecar.
_REQUIRED_KEYS = ("serial", "session_name", "start_time_unix")


def _is_mcap_part(name: str) -> bool:
    """One decision about what counts as a part, because it is load-bearing: the
    uploader parks ``<part>.mcap.uploaded`` tombstones in the same folder, and those
    must not be mistaken for data. Case-insensitive — a card written elsewhere can
    carry ``.MCAP``.
    """
    return name.lower().endswith(".mcap")


def _take_bytes(buf: bytes, at: int, end: int) -> tuple[bytes, int] | None:
    """A u32-length-prefixed string as ``(value, next_offset)``, or None past ``end``."""
    if end - at < 4:
        return None
    (n,) = struct.unpack_from("<I", buf, at)
    at += 4
    if end - at < n:
        return None
    return buf[at:at + n], at + n


def _parse_metadata_record(payload: bytes) -> dict[str, str] | None:
    """Key/values of a Metadata record payload, or None if it isn't the capture one."""
    end = len(payload)
    taken = _take_bytes(payload, 0, end)
    if taken is None:
        return None
    name, at = taken
    if name.decode("utf-8", "replace") != _CAPTURE_RECORD:
        return None
    if end - at < 4:
        return None
    (map_len,) = struct.unpack_from("<I", payload, at)
    at += 4
    if end - at < map_len:      # over-claimed length: rejected, as the device reader does
        return None
    map_end = at + map_len
    kv: dict[str, str] = {}
    while at < map_end:
        key = _take_bytes(payload, at, map_end)
        if key is None:
            break
        raw_key, at = key
        value = _take_bytes(payload, at, map_end)
        if value is None:
            break
        raw_value, at = value
        kv[raw_key.decode("utf-8", "replace")] = raw_value.decode("utf-8", "replace")
    return kv


def read_capture_metadata(path: str | Path) -> dict[str, str] | None:
    """The ``visio.capture`` key/values embedded in an ``.mcap``, or None if absent.

    None covers every way a file can fail to yield them — not an MCAP, no record,
    truncated, or a record so torn it lost its identity keys — because the caller's
    answer is the same in each case: try the next part.

    Every length in the file is treated as hostile. A corrupt u64 does not land
    harmlessly past EOF: seeking by one raises `ValueError` (not `OSError`), which
    would escape the caller's guard and kill a whole card mid-run.

    Args:
        path: Path to one ``.mcap`` part.

    Returns:
        The metadata mapping, or None.

    Raises:
        OSError: If the file can't be opened or read.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if f.read(8) != _MAGIC:
            return None
        for _ in range(_MAX_RECORDS):
            header = f.read(9)
            if len(header) != 9:
                return None
            opcode = header[0]
            (length,) = struct.unpack_from("<Q", header, 1)
            if opcode in _OP_DATA_BEGINS or length > size:
                return None
            if opcode != _OP_METADATA:
                f.seek(length, 1)
                continue
            if not _MIN_PAYLOAD <= length <= _MAX_PAYLOAD:
                return None
            payload = f.read(length)
            if len(payload) != length:
                return None
            kv = _parse_metadata_record(payload)
            if kv and all(k in kv for k in _REQUIRED_KEYS):
                return kv
    return None


def _quote(text: str) -> str:
    # ensure_ascii=False: the firmware wrote raw UTF-8 through (task/capturer are
    # routinely Chinese), and the sidecar is read back as UTF-8.
    return json.dumps(text, ensure_ascii=False)


def _real(meta: dict[str, str], key: str, digits: int) -> str:
    value = float(meta[key]) if key in meta else 0.0
    if not math.isfinite(value):
        raise ValueError(f"{key}={meta[key]!r}")   # nan/inf renders unparseable JSON
    return format(value, f".{digits}f")


def _integer(meta: dict[str, str], key: str) -> str:
    # int(), not int(float()): the firmware writes these with std::to_string on an
    # integer, and client_unix_us is a microsecond epoch that a float64 would round.
    return str(int(meta[key])) if key in meta else "0"


def session_json_text(meta: dict[str, str]) -> str:
    """Render the sidecar for one session's capture metadata.

    Key order and number formatting reproduce the firmware's printf exactly — that
    layout is what downstream parsers were built against — and every key is always
    present, as it was in the original file. The MCAP record omits the empty/zero
    ones, so a missing key renders as the blank the sidecar always carried. The one
    renamed field is ``serial`` in the record, which was ``device_id`` in the sidecar.

    Fields added after the layout was frozen go at the END, matching the firmware's
    own table: everything ahead of them stays byte-identical, so a recording made
    before they existed rebuilds to the same prefix it always did, blanks and all.

    Args:
        meta: Key/values from `read_capture_metadata`.

    Returns:
        The file's contents — one JSON object on a single line, newline-terminated.

    Raises:
        ValueError: If a number in the record is unparseable or not finite. Present
            but malformed is a damaged record, not a blank field, and rendering it as
            ``0.000000`` would launder corruption into a plausible timestamp.
    """
    fields = (
        ("session_name", _quote(meta.get("session_name", ""))),
        ("device_id", _quote(meta.get("serial", ""))),
        ("hostname", _quote(meta.get("hostname", ""))),
        ("kernel", _quote(meta.get("kernel", ""))),
        ("app_version", _quote(meta.get("app_version", ""))),
        ("start_time_unix", _real(meta, "start_time_unix", 6)),
        ("task", _quote(meta.get("task", ""))),
        ("location", _quote(meta.get("location", ""))),
        ("message", _quote(meta.get("message", ""))),
        ("capturer", _quote(meta.get("capturer", ""))),
        ("latitude", _real(meta, "latitude", 7)),
        ("longitude", _real(meta, "longitude", 7)),
        ("client_unix_us", _integer(meta, "client_unix_us")),
        ("client_utc_offset_min", _integer(meta, "client_utc_offset_min")),
        ("fps", _integer(meta, "fps")),
        ("operator_id", _quote(meta.get("operator_id", ""))),
        ("environment_id", _quote(meta.get("environment_id", ""))),
        # Which key opens this session's parts; empty means they are plaintext.
        # Deliberately not paired with an `encrypted` bool — two fields could
        # disagree and nothing would say which wins. The value is
        # SHA-256(key)[:8]: it names the key, it cannot decrypt anything, and
        # the same bytes sit in every part's VREC header.
        ("recording_key_fingerprint",
         _quote(meta.get("recording_key_fingerprint", ""))),
    )
    return "{" + ",".join(f"{_quote(k)}:{v}" for k, v in fields) + "}\n"


def mcap_parts(session_dir: Path) -> list[Path]:
    """The session's ``.mcap`` parts, in name order.

    Name before `is_file`, so a folder of JPEGs costs one `stat` per part rather than
    one per file — on a network archive each of those is a round trip.
    """
    return sorted(p for p in session_dir.iterdir()
                  if _is_mcap_part(p.name) and p.is_file())


def session_text(parts: list[Path]) -> tuple[str | None, str]:
    """Render one session's sidecar from its parts: ``(text, source-or-reason)``.

    Every part is tried. Part 0 of a session torn by a power cut can be truncated,
    unreadable, or damaged past its identity keys, while the rolled parts that
    followed each re-emit the record intact — so a bad first part must not decide the
    session. ``text`` is None when none of them yielded usable metadata, and the
    second element then says why, naming the parts that failed and how.
    """
    problems: list[str] = []
    for part in parts:
        try:
            meta = read_capture_metadata(part)
        except OSError as exc:
            problems.append(f"{part.name}: {exc}")
            continue
        if meta is None:
            continue
        try:
            return session_json_text(meta), part.name
        except ValueError as exc:
            problems.append(f"{part.name}: damaged capture metadata ({exc})")
    if problems:
        return None, "no usable capture metadata — " + "; ".join(problems)
    return None, ("no capture metadata in any .mcap part — recorded by firmware older "
                  "than the in-MCAP metadata, whose sessions still have their sidecar")


def rebuild_session(session_dir: Path, parts: list[Path] | None = None, *,
                    force: bool = False, dry_run: bool = False) -> tuple[str, str]:
    """Write ``session_dir/session.json`` from the session's own MCAP parts.

    Args:
        session_dir: A folder holding the session's ``.mcap`` parts.
        parts: Its parts, when the caller already listed them; otherwise found here.
        force: Overwrite an existing sidecar instead of keeping it.
        dry_run: Report what would happen; write nothing.

    Returns:
        ``(status, message)`` — status is `REBUILT`, `KEPT` or `FAILED`.
    """
    out = session_dir / SIDECAR_NAME
    if not force and out.exists():
        return KEPT, "kept the existing session.json (--force to overwrite)"
    text, detail = session_text(mcap_parts(session_dir) if parts is None else parts)
    if text is None:
        # Under --force the sidecar was never checked; an old session that has one
        # and nothing to rebuild it from is intact, not a failure.
        if out.exists():
            return KEPT, f"kept the existing session.json ({detail})"
        return FAILED, detail
    if dry_run:
        return REBUILT, f"would rebuild from {detail}"
    try:
        out.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        return FAILED, f"cannot write {SIDECAR_NAME}: {exc}"
    return REBUILT, f"rebuilt from {detail}"


def find_sessions(root: Path, *, on_error: Callable[[OSError], None] | None = None,
                  on_scan: Callable[[str], None] | None = None,
                  ) -> Iterator[tuple[Path, list[Path]]]:
    """Yield ``(folder, parts)`` for every folder under ``root`` holding ``.mcap`` parts.

    Handles all three things a user points this at: one session folder, a whole card
    (or an archive tree) of them, and a folder that is both.

    A generator over ``os.walk``, so the caller can report each session the moment it
    is found: pointed at a big tree by mistake — a home directory, a source checkout —
    a collect-then-work scan is indistinguishable from a hang. Hidden folders are
    skipped (``.git`` and friends hold no recordings) and each folder is listed once,
    the parts coming straight from the walk.

    ``on_error`` is called for a directory that can't be read. It must not be dropped:
    ``os.walk`` silently omits those by default, which would let the tool skip a whole
    branch of a card and still report success.
    """
    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        if on_scan is not None:
            on_scan(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        parts = sorted(f for f in filenames if _is_mcap_part(f))
        if parts:
            yield Path(dirpath), [Path(dirpath, p) for p in parts]


def _owns_the_console() -> bool:
    """True when this process owns its console window — i.e. it was double-clicked or
    had a folder dropped on it, and that window closes the instant we return.

    Windows only: `GetConsoleProcessList` counts the processes attached to the
    console, so 1 means nobody (no cmd.exe, no CI runner) is there to keep it open.
    The `isatty` test covers a wrapper that spawned us with CREATE_NEW_CONSOLE and
    redirected our streams — nobody would be at that keyboard either.
    """
    if sys.platform != "win32" or not sys.stdin.isatty():
        return False
    import ctypes

    pids = (ctypes.c_uint * 2)()
    return ctypes.windll.kernel32.GetConsoleProcessList(pids, 2) == 1


def _print_json(text: str) -> None:
    # Straight to the byte stream: a legacy Windows console codepage (cp1252/cp936)
    # would mangle a non-ASCII task or capturer on the way out, including when the
    # caller is redirecting this into a file.
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit status (0 = nothing failed)."""
    parser = argparse.ArgumentParser(
        prog="visio-session-json",
        description="Rebuild a recording session's session.json sidecar from its .mcap files.",
    )
    parser.add_argument(
        # The default is already a Path: argparse applies `type` to what it parses,
        # never to a default that isn't a bare string — and the no-argument run is
        # the double-click case, the one nobody types.
        "paths", nargs="*", default=[Path(".")], metavar="PATH", type=Path,
        help="a session folder, a folder of sessions (including every subfolder), or "
             "a single .mcap file (default: the current folder)")
    parser.add_argument("--force", action="store_true",
                        help=f"overwrite an existing {SIDECAR_NAME}")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be rebuilt; write nothing")
    parser.add_argument("--stdout", action="store_true",
                        help="print the JSON instead of writing any sidecar "
                             "(ignores --force and --dry-run)")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:                                  # progress lines carry the capturer name
            stream.reconfigure(errors="replace")
        except AttributeError:                # not a real stream (pytest's capture)
            pass

    counts = {REBUILT: 0, KEPT: 0, FAILED: 0}

    def handle(session: Path, parts: list[Path] | None = None) -> None:
        if args.stdout:
            text, detail = session_text(mcap_parts(session) if parts is None else parts)
            if text is None:
                print(f"{session}: {detail}", file=sys.stderr)
                counts[FAILED] += 1
            else:
                _print_json(text)
            return
        status, message = rebuild_session(session, parts, force=args.force,
                                          dry_run=args.dry_run)
        print(f"{session}: {message}",
              file=sys.stderr if status == FAILED else sys.stdout, flush=True)
        counts[status] += 1

    def walk(path: Path) -> None:
        # stderr throughout: progress, not data — `--stdout | jq` must stay clean.
        print(f"searching {path} ...", file=sys.stderr, flush=True)
        last = time.monotonic()

        def note_error(exc: OSError) -> None:
            print(f"{exc.filename}: cannot search this folder: {exc.strerror}",
                  file=sys.stderr, flush=True)
            counts[FAILED] += 1

        def note_scan(dirpath: str) -> None:
            # A tree with no recordings in it prints nothing for its whole traversal,
            # which is the symptom this tool must never show again.
            nonlocal last
            if time.monotonic() - last > 2.0:
                print(f"  ... {dirpath}", file=sys.stderr, flush=True)
                last = time.monotonic()

        found = 0
        for session, parts in find_sessions(path, on_error=note_error, on_scan=note_scan):
            found += 1
            last = time.monotonic()
            handle(session, parts)
        if not found:
            print(f"{path}: contains no .mcap files", file=sys.stderr)
            counts[FAILED] += 1

    for path in args.paths:
        if path.is_file():
            handle(path.parent)     # any file identifies its session folder
        elif path.is_dir():
            walk(path)
        else:
            print(f"{path}: no such file or folder", file=sys.stderr)
            counts[FAILED] += 1

    if not args.stdout:
        verb = "would rebuild" if args.dry_run else "rebuilt"
        print(f"{verb} {counts[REBUILT]}, kept {counts[KEPT]}, failed {counts[FAILED]}")
    if _owns_the_console():
        try:
            print("\nPress Enter to close...", file=sys.stderr, end="", flush=True)
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    return 1 if counts[FAILED] else 0


if __name__ == "__main__":
    sys.exit(main())
