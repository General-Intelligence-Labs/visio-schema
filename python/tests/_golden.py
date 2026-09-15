"""Load a tests/golden/*.txt vector file.

Extracted so the suites that read one stop each carrying their own parser --
which is the same drift the fixtures exist to prevent, one level up.

Grammar: `#` comments, blank lines, and `key=lowercase_hexbytes`.
"""
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "tests" / "golden"


def load(name: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for line in (GOLDEN_DIR / name).read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, hexbytes = line.partition("=")
        out[key] = bytes.fromhex(hexbytes)
    return out
