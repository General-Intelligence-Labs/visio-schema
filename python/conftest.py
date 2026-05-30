"""Test-time shim so the hand-written codec and the generated bindings
resolve as one `visio` namespace package.

The generated protobuf modules live in ../gen/python (built by
`make gen`); the hand-written codec lives here under ./visio. Both
contribute to the `visio.*` namespace. This adds both roots to sys.path
so `import visio.wire.codec` and `import visio.wire.v1.header_pb2` work
together before a combined wheel is installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_GEN_PY = _HERE.parent / "gen" / "python"

for _root in (_HERE, _GEN_PY):
    if _root.is_dir() and str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
