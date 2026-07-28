"""visio-session-json — rebuild a recording's ``session.json`` sidecar from its MCAP.

Devices used to drop a small ``session.json`` next to the ``.mcap`` parts of every
recording session. The firmware now writes that capture metadata *inside* the MCAP
instead (the ``visio.capture`` Metadata record), so a file copied off the card is
self-contained and the sidecar is gone. This rebuilds it — same keys, same order,
same number formatting — for pipelines that still read it::

    visio-session-json D:\\recordings           # a card, a session folder, or one .mcap
    python session_json.py ~/recordings         # same, with any stock Python 3.10+

Point it at anything: one ``.mcap``, one session folder, or a whole card — every
session below is rebuilt. On Windows the frozen build is also drag-and-drop (drop a
folder on the .exe) and double-click (rebuilds the folder it sits in), and it holds
its console window open so the report can be read.

One file, stdlib only, on purpose — it reads the few MCAP record bytes it needs
rather than importing the ``mcap`` library, so it can be mailed to a customer and
run as-is. Sidecars are always written UTF-8 with ``\\n`` endings, whatever the
console codepage: a task or capturer name is routinely non-ASCII.

Only sessions recorded by firmware that embeds the record can be rebuilt; older
recordings predate it — and they still have their original sidecar.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from collections.abc import Iterator
from pathlib import Path

__all__ = ["SIDECAR_NAME", "read_capture_metadata", "rebuild_session", "session_json_text"]

SIDECAR_NAME = "session.json"

# MCAP framing (mcap.dev spec): file = MAGIC, records, MAGIC; each top-level record
# is [opcode u8][length u64le][payload]. The recorder emits the capture Metadata
# record right after the Header — record 1 — and re-emits it in every rolled part,
# so the walk below stops long before the message data.
_MAGIC = b"\x89MCAP0\r\n"
_OP_METADATA = 0x0C
_CAPTURE_RECORD = "visio.capture"
_MAX_RECORDS = 256
_MAX_PAYLOAD = 1 << 20


def _take_bytes(buf: bytes, at: int, end: int) -> tuple[bytes, int] | None:
    """A u32-length-prefixed string as ``(value, next_offset)``, or None past ``end``.

    Every length is bounds-checked against the enclosing record rather than trusted:
    a torn part (power cut mid-write) can carry a garbage u32.
    """
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
    map_end = min(at + map_len, end)
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

    Args:
        path: Path to one ``.mcap`` part.

    Returns:
        The metadata mapping, or None when the file isn't an MCAP, carries no capture
        record, or is truncated before one.

    Raises:
        OSError: If the file can't be opened or read.
    """
    with open(path, "rb") as f:
        if f.read(8) != _MAGIC:
            return None
        for _ in range(_MAX_RECORDS):
            header = f.read(9)
            if len(header) != 9:
                break
            (length,) = struct.unpack_from("<Q", header, 1)
            if header[0] != _OP_METADATA:
                f.seek(length, 1)   # past EOF is fine — the next read returns b""
                continue
            if length > _MAX_PAYLOAD:
                break
            payload = f.read(length)
            if len(payload) != length:
                break
            kv = _parse_metadata_record(payload)
            if kv is not None:
                return kv
    return None


def _quote(text: str) -> str:
    # ensure_ascii=False: the firmware wrote raw UTF-8 through (task/capturer are
    # routinely Chinese), and the sidecar is read back as UTF-8.
    return json.dumps(text, ensure_ascii=False)


def session_json_text(meta: dict[str, str], *, session_name: str = "") -> str:
    """Render the sidecar for one session's capture metadata.

    Key order and number formatting reproduce the firmware's printf exactly — that
    layout is what downstream parsers were built against — and every key is always
    present, as it was in the original file. The MCAP record omits the empty/zero
    ones, so a missing key renders as the blank the sidecar always carried. The one
    renamed field is ``serial`` in the record, which was ``device_id`` in the sidecar.

    Args:
        meta: Key/values from `read_capture_metadata`.
        session_name: Fallback name (the session folder) if the record lacks one.

    Returns:
        The file's contents — one JSON object on a single line, newline-terminated.
    """
    def text(key: str) -> str:
        return _quote(meta.get(key, ""))

    def real(key: str, digits: int) -> str:
        try:
            return format(float(meta[key]), f".{digits}f")
        except (KeyError, ValueError):
            return format(0.0, f".{digits}f")

    def integer(key: str) -> str:
        try:
            return str(int(float(meta[key])))
        except (KeyError, ValueError):
            return "0"

    fields = (
        ("session_name", _quote(meta.get("session_name") or session_name)),
        ("device_id", text("serial")),
        ("hostname", text("hostname")),
        ("kernel", text("kernel")),
        ("app_version", text("app_version")),
        ("start_time_unix", real("start_time_unix", 6)),
        ("task", text("task")),
        ("location", text("location")),
        ("message", text("message")),
        ("capturer", text("capturer")),
        ("latitude", real("latitude", 7)),
        ("longitude", real("longitude", 7)),
        ("client_unix_us", integer("client_unix_us")),
        ("client_utc_offset_min", integer("client_utc_offset_min")),
        ("fps", integer("fps")),
    )
    return "{" + ",".join(f"{_quote(k)}:{v}" for k, v in fields) + "}\n"


def mcap_parts(session_dir: Path) -> list[Path]:
    """The session's ``.mcap`` parts, in name order (``.suffix`` — Windows may shout it)."""
    return sorted(p for p in session_dir.iterdir()
                  if p.is_file() and p.suffix.lower() == ".mcap")


def find_sessions(root: Path) -> Iterator[Path]:
    """Yield every folder at or under ``root`` holding at least one ``.mcap`` part.

    Handles all three things a user points this at: one session folder, a whole card
    (or an archive tree) of them, and a folder that is both.

    A generator over ``os.walk``, so the caller can report each session the moment it
    is found: pointed at a big tree by mistake — a home directory, a source checkout —
    a collect-then-work scan looks indistinguishable from a hang. Hidden folders are
    skipped (``.git`` and friends hold no recordings) and each folder is read once.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if any(f.lower().endswith(".mcap") for f in filenames):
            yield Path(dirpath)


def rebuild_session(session_dir: Path, *, force: bool = False,
                    dry_run: bool = False) -> tuple[bool, str]:
    """Write ``session_dir/session.json`` from the session's own MCAP parts.

    Later parts are tried when the first carries no record: part 0 of a session torn
    by a power cut can be truncated before its metadata, while the rolled parts that
    followed each re-emit it.

    Args:
        session_dir: A folder holding the session's ``.mcap`` parts.
        force: Overwrite an existing sidecar instead of keeping it.
        dry_run: Report what would happen; write nothing.

    Returns:
        ``(ok, message)`` — ``ok`` is False only when nothing could be rebuilt.
    """
    out = session_dir / SIDECAR_NAME
    if out.exists() and not force:
        return True, "kept the existing session.json (--force to overwrite)"
    for part in mcap_parts(session_dir):
        try:
            meta = read_capture_metadata(part)
        except OSError as exc:
            return False, f"cannot read {part.name}: {exc}"
        if not meta:
            continue
        text = session_json_text(meta, session_name=session_dir.name)
        if dry_run:
            return True, f"would rebuild from {part.name}"
        try:
            out.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            return False, f"cannot write {SIDECAR_NAME}: {exc}"
        return True, f"rebuilt from {part.name}"
    return False, ("no capture metadata in any .mcap part — recorded by firmware older "
                   "than the in-MCAP metadata, whose sessions still have their sidecar")


def _launched_from_explorer() -> bool:
    """True when this process owns its console window — i.e. it was double-clicked or
    had a folder dropped on it, and that window closes the instant we return.

    Windows only, and only meaningful frozen: `GetConsoleProcessList` counts the
    processes attached to the console, so 1 means nobody (no cmd.exe, no CI runner)
    is there to keep it open. Run from a shell, the count is ≥2 and we don't pause.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
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
        help="a session folder, a folder of sessions (searched top to bottom), or a "
             "single .mcap file (default: the current folder)")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing session.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be rebuilt; write nothing")
    parser.add_argument("--stdout", action="store_true",
                        help="print the JSON instead of writing any sidecar")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:                                  # progress lines carry the capturer name
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):     # not a real console (pytest, a pipe)
            pass

    written = kept = failed = 0

    def handle(session: Path) -> None:
        nonlocal written, kept, failed
        if args.stdout:
            parts = mcap_parts(session)
            meta = next((m for m in (read_capture_metadata(p) for p in parts) if m), None)
            if meta is None:
                print(f"{session}: no capture metadata in any .mcap part", file=sys.stderr)
                failed += 1
                return
            _print_json(session_json_text(meta, session_name=session.name))
            return
        ok, message = rebuild_session(session, force=args.force, dry_run=args.dry_run)
        print(f"{session}: {message}", file=sys.stdout if ok else sys.stderr, flush=True)
        if not ok:
            failed += 1
        elif message.startswith("kept"):
            kept += 1
        else:
            written += 1

    for path in args.paths:
        if path.is_file():
            handle(path.parent if path.suffix.lower() == ".mcap" else path)
        elif path.is_dir():
            # Announced, and each session reported as the walk reaches it: pointed at
            # a whole home directory or source tree, this can take a while, and a
            # silent scan is indistinguishable from a hang.
            # stderr: progress, not data — `--stdout | jq` must stay clean.
            print(f"searching {path} ...", file=sys.stderr, flush=True)
            found = 0
            for session in find_sessions(path):
                found += 1
                handle(session)
            if not found:
                print(f"{path}: no .mcap files found under here", file=sys.stderr)
                failed += 1
        else:
            print(f"{path}: no such file or folder", file=sys.stderr)
            failed += 1

    if not args.stdout:
        verb = "would rebuild" if args.dry_run else "rebuilt"
        print(f"{verb} {written}, kept {kept}, failed {failed}")
    if _launched_from_explorer():
        try:
            input("\nPress Enter to close...")
        except (EOFError, KeyboardInterrupt):
            pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
