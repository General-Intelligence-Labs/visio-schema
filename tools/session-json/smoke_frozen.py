"""Acceptance check for a FROZEN visio-session-json build (the .exe CI ships).

    python tools/session-json/smoke_frozen.py dist

The unit suite proves the logic; this proves the *bundle* — that PyInstaller's
bootloader, the frozen stdlib and, above all, the Windows filesystem and console
paths still produce the same bytes. So it deliberately uses a folder named with a
space and CJK characters, and a capturer name that isn't ASCII: those are ordinary
for this fleet, and they are exactly what a cp936/cp1252 console mangles.

Runs the binary through subprocess with an argv list — no shell in the middle to
re-encode the path — and checks all three outcomes the tool promises: rebuilt, kept,
and a failure that exits 1.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python" / "tests"))
from test_session_json import GOLDEN, GOLDEN_META, _mcap  # noqa: E402

CAPTURER = "周兴波"
SIDECAR = GOLDEN.replace('"capturer":""', f'"capturer":"{CAPTURER}"').encode("utf-8")


def run(exe: Path, *args: str) -> subprocess.CompletedProcess:
    done = subprocess.run([str(exe), *args], capture_output=True)
    sys.stdout.write(done.stdout.decode("utf-8", "replace"))
    sys.stderr.write(done.stderr.decode("utf-8", "replace"))
    return done


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    found = [p for p in dist.iterdir() if p.stem == "visio-session-json"]
    if len(found) != 1:
        raise SystemExit(f"expected one binary in {dist}, found {found}")
    exe = found[0]

    if run(exe, "--help").returncode != 0:
        raise SystemExit("--help failed")

    card = Path("smoke") / "My Recordings" / "配菜"
    session = card / "session_00003"
    session.mkdir(parents=True)
    (session / "ego_0000.mcap").write_bytes(_mcap({**GOLDEN_META, "capturer": CAPTURER}))
    empty = card / "session_00004"          # no metadata: the failure path
    empty.mkdir()
    (empty / "ego_0000.mcap").write_bytes(_mcap(None))

    # 1. rebuild — a failure anywhere on the card must still exit 1
    if run(exe, str(card)).returncode != 1:
        raise SystemExit("a card with one unrebuildable session must exit 1")
    written = (session / "session.json").read_bytes()
    if written != SIDECAR:
        raise SystemExit(f"sidecar mismatch:\n  got {written!r}\n  want {SIDECAR!r}")

    # 2. run again — the sidecar is kept, byte-identical, not rewritten
    again = run(exe, str(session))
    if again.returncode != 0 or b"kept" not in again.stdout:
        raise SystemExit("a second run must keep the existing sidecar")
    if (session / "session.json").read_bytes() != SIDECAR:
        raise SystemExit("the kept sidecar changed")

    # 3. --stdout is the same bytes, and stays clean of progress output
    piped = run(exe, "--stdout", str(session))
    if piped.stdout != SIDECAR:
        raise SystemExit(f"--stdout mismatch: {piped.stdout!r}")

    print(f"frozen build OK: {exe.name} rebuilt, kept, and printed the golden sidecar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
