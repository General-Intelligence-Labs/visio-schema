"""Test-time shim: put the package root on sys.path.

Generated bindings and the hand-written codec now live together under
`python/visio_schema` (one PEP 420 namespace package), so a single path entry
makes both `import visio_schema.wire.codec` and
`import visio_schema.wire.v1.header_pb2` resolve. Run `make gen` first to
populate the generated modules.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
