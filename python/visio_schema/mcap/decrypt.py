"""``visio-decrypt`` — write a plaintext copy of an encrypted recording.

MANDATORY, not a convenience. Everything that reads a recording through
``visio_schema`` learned the container in one line (``read_mcap``), but the
tools an admin actually reaches for did not and cannot:

  * **Foxglove Studio** opens ``.mcap`` natively and is the only good scrubber.
  * the **kalibr** calibration path shells out to third-party binaries.
  * the frozen **visio-setup** PyInstaller bundle ships its own interpreter.

None of those will ever grow a key flag, so the way to use them is a plaintext
copy on the side. Streams in fixed-size chunks — a shift's recording does not
fit in memory, and neither does a 2 GB part.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from visio_schema.mcap.crypto import (
    HEADER_BYTES,
    RecordingKeyMismatch,
    RecordingKeyUnavailable,
    is_vrec,
    open_recording,
    read_vrec_header,
)

__all__ = ["main", "run"]

_CHUNK = 4 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="visio-decrypt",
        description="Write a plaintext MCAP copy of an encrypted (VREC) "
                    "recording, for tools that cannot take a key.",
    )
    p.add_argument("input", help="the recording, encrypted or not")
    p.add_argument("output", help="plaintext .mcap to write")
    p.add_argument("--key", metavar="HEX",
                   help="recording key, 64 hex chars. Prefer --key-file: an "
                        "argument is visible in `ps` to every user")
    p.add_argument("--key-file", metavar="PATH",
                   help="file holding the key as 64 hex chars")
    args = p.parse_args(argv)

    src, dst = Path(args.input), Path(args.output)
    if args.key and args.key_file:
        p.error("--key and --key-file are mutually exclusive")
    key: bytes | str | None = args.key
    if args.key_file:
        key = Path(args.key_file).expanduser().read_text().strip()

    # Refuse rather than silently clobber: the output is a full second copy of
    # a recording, and overwriting the wrong path can cost a whole shift.
    if dst.exists():
        print(f"visio-decrypt: {dst} exists — refusing to overwrite",
              file=sys.stderr)
        return 2

    try:
        with open(src, "rb") as probe:
            encrypted = is_vrec(probe.read(HEADER_BYTES))
        if not encrypted:
            # Say so and copy anyway: in a directory of parts an admin should
            # not have to sort the plaintext ones out by hand, and a rotation
            # leaves exactly that mix.
            print(f"visio-decrypt: {src.name} is already plaintext — copying",
                  file=sys.stderr)
            shutil.copyfile(src, dst)
            return 0

        with open(src, "rb") as probe:
            header = read_vrec_header(probe.read(HEADER_BYTES))
        with open_recording(src, key) as fin, open(dst, "wb") as fout:
            while chunk := fin.read(_CHUNK):
                fout.write(chunk)
        print(f"visio-decrypt: {src.name} (key {header.key_fp_hex}) -> {dst}",
              file=sys.stderr)
        return 0
    except (RecordingKeyUnavailable, RecordingKeyMismatch) as exc:
        print(f"visio-decrypt: {exc}", file=sys.stderr)
        dst.unlink(missing_ok=True)
        return 1
    except OSError as exc:
        print(f"visio-decrypt: {exc}", file=sys.stderr)
        dst.unlink(missing_ok=True)
        return 1


def run() -> None:
    """Console entry point."""
    raise SystemExit(main())
