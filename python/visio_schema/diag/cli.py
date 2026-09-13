"""``visio-diag`` — open, render and summarise a device diagnostic log.

    visio-diag open     GILABS-7K3M9QP2.3.vdlg          # the lines, decrypted
    visio-diag render   GILABS-7K3M9QP2.3.vdlg          # per-run summary + every W/E line
    visio-diag counters GILABS-7K3M9QP2.vitals.txt      # the lifetime counters

The key comes from ``--key`` / ``--key-file``, then ``$VISIO_DIAG_KEY``,
``$VISIO_DIAG_KEY_FILE``, ``~/.config/visio/diag-key`` — see
``visio_schema.diag.resolve_diag_key``. A plaintext log (what a dev image with
no key staged writes) needs none.

Same shape as ``visio-decrypt``: ``main`` returns the exit status, ``run`` is
the console entry.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from visio_schema.diag import DiagRecord, iter_records, resolve_diag_key, split_boots
from visio_schema.mcap.crypto import (
    RecordingKeyMismatch,
    RecordingKeyUnavailable,
    add_key_args,
    key_from_args,
)

_PROG = "visio-diag"


def _key_from(args: argparse.Namespace) -> bytes | None:
    return resolve_diag_key(key_from_args(args))


def _wall(r: DiagRecord) -> str:
    return r.wall.strftime("%Y-%m-%d %H:%M:%S") + "Z" if r.wall else "(no clock)"


def cmd_open(args: argparse.Namespace) -> int:
    for r in iter_records(args.file, _key_from(args)):
        print(r.raw)
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    records = list(iter_records(args.file, _key_from(args)))
    runs = split_boots(records)
    unparsed = sum(1 for r in records if not r.parsed)
    print(f"{Path(args.file).name}: {len(records)} lines, {len(runs)} run(s)"
          + (f", {unparsed} unparseable" if unparsed else ""))
    for i, run in enumerate(runs, 1):
        parsed = [r for r in run if r.parsed]
        boot = next((r for r in run if r.tag == "BOOT"), None)
        walls = [r for r in parsed if r.wall]
        sev = Counter(r.severity for r in parsed)
        print()
        print(f"── run {i}: {len(run)} lines"
              + (f"  {boot.message}" if boot else "  (no [BOOT] banner)"))
        if parsed:
            span = parsed[-1].mono_s - parsed[0].mono_s
            print(f"   uptime +{parsed[0].mono_s:.0f}s → +{parsed[-1].mono_s:.0f}s"
                  f" ({span / 3600:.1f} h in this file)")
        if walls:
            print(f"   wall   {_wall(walls[0])} → {_wall(walls[-1])}")
        else:
            print("   wall   never set this run")
        print("   " + "  ".join(f"{s}={sev[s]}" for s in "DIWE" if sev[s]))
        tags = Counter(r.tag for r in parsed if r.tag)
        if tags:
            print("   tags   " + ", ".join(f"{t}={n}" for t, n in tags.most_common(6)))
        flagged = [r for r in parsed if r.severity in "WE"]
        for r in flagged:
            text = f"[{r.tag}] {r.message}" if r.tag else r.message
            print(f"   {r.severity} +{r.mono_s:11.3f}  {text}")
    return 0


def cmd_counters(args: argparse.Namespace) -> int:
    rows: list[tuple[str, str]] = []
    for line in Path(args.file).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        rows.append((k.strip(), v.strip()))
    if not rows:
        print(f"{_PROG}: {args.file}: no counters", file=sys.stderr)
        return 1
    width = max(len(k) for k, _ in rows)
    for k, v in sorted(rows):
        print(f"{k:<{width}}  {v}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=_PROG, description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_key(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("file")
        add_key_args(sp, what="diagnostic key")

    with_key(sub.add_parser("open", help="print the decrypted lines"))
    with_key(sub.add_parser("render", help="per-run summary and every W/E line"))
    sub.add_parser("counters", help="print a vitals.txt").add_argument("file")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {"open": cmd_open, "render": cmd_render, "counters": cmd_counters}[args.cmd]
    try:
        return handler(args)
    except (RecordingKeyUnavailable, RecordingKeyMismatch, ValueError, OSError) as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1


def run() -> None:
    """Console entry point."""
    raise SystemExit(main())


if __name__ == "__main__":  # python -m visio_schema.diag.cli
    run()
