"""``python -m visio_schema.settings_qr`` — the console script's module form.

Mirrors :mod:`visio_schema.display`. Callers that cannot rely on a console
script being on PATH — a test invoking it from a sibling checkout, a CI step
running against the source tree — need this entry point rather than
reconstructing the import in a ``-c`` one-liner.
"""
from . import run

run()
