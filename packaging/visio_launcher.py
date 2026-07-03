"""PyInstaller entry point — freezes to the ``visio-display`` app.

Double-clicking the app passes no CLI arguments, so with none we default to the
``--serve`` launcher (discover devices → open Foxglove). Advanced users can still
pass ``--serial`` / ``--tcp`` / etc. from a terminal. (The ``--rerun`` sink is
excluded from this bundle to keep it small — see the spec.)"""
import sys

from visio_schema.display import run

if len(sys.argv) == 1:      # double-clicked / launched with no args → the launcher UI
    sys.argv.append("--serve")

run()
