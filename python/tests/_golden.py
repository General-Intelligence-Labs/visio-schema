"""Load a tests/golden/*.txt vector file.

Extracted so the suites that read one stop each carrying their own parser --
which is the same drift the fixtures exist to prevent, one level up.

Grammar: `#` comments, blank lines, and `key=lowercase_hexbytes`.
"""
import importlib.util
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO / "tests" / "golden"


def load(name: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for line in (GOLDEN_DIR / name).read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, hexbytes = line.partition("=")
        out[key] = bytes.fromhex(hexbytes)
    return out


def load_generator(script: str) -> ModuleType:
    """Import scripts/<script>.py, the generator a golden file comes from."""
    spec = importlib.util.spec_from_file_location(
        script, REPO / "scripts" / f"{script}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
