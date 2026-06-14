"""Vendor the sibling C++/nanopb sources the native `_creader` extension compiles
into ``python/_native_build/`` so they live UNDER the setup.py directory.

setuptools rejects Extension sources outside the setup.py dir (``..`` paths), so a
distributable wheel can't reference ``../cpp`` / ``../third_party`` directly. This
copies the needed trees in. It runs automatically from setup.py when the siblings
are present (editable / in-place builds); CI runs it explicitly on the host before
cibuildwheel (which copies only ``python/`` into its build container, so the
vendored tree must already exist).

No-op when ``../cpp`` is absent (e.g. inside the cibuildwheel container) — the
vendored tree is then assumed already populated.
"""
from __future__ import annotations

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.dirname(HERE)
CPP = os.path.join(SCHEMA, "cpp")
NANOPB = os.path.join(SCHEMA, "third_party", "nanopb")
VENDOR = os.path.join(HERE, "_native_build")


def vendor() -> bool:
    """Refresh ``_native_build/`` from the sibling sources; return whether it ran.

    No-op when ``cpp/`` is absent (the cibuildwheel container, where the tree is
    pre-vendored). But ``cpp/`` present while nanopb is missing is an
    uninitialized submodule, not the container case — warn and skip (degrading to
    the pure-Python reader) rather than silently building as if nothing's wrong.
    """
    if not os.path.isdir(CPP):
        return False
    if not os.path.isfile(os.path.join(NANOPB, "pb_decode.c")):
        print(f"warning: nanopb sources missing at {NANOPB} — run "
              "`git submodule update --init third_party/nanopb`; skipping the "
              "native _creader extension", file=sys.stderr)
        return False
    # Whole-subtree copies keep this robust to dependency changes; setup.py lists
    # only the files it compiles.
    shutil.copytree(os.path.join(CPP, "include"),
                    os.path.join(VENDOR, "include"), dirs_exist_ok=True)
    shutil.copytree(os.path.join(CPP, "generated_nanopb"),
                    os.path.join(VENDOR, "generated_nanopb"), dirs_exist_ok=True)
    shutil.copytree(os.path.join(CPP, "src"),
                    os.path.join(VENDOR, "src"), dirs_exist_ok=True)
    os.makedirs(os.path.join(VENDOR, "nanopb"), exist_ok=True)
    for name in os.listdir(NANOPB):
        if name.endswith((".c", ".h")):
            shutil.copy2(os.path.join(NANOPB, name),
                         os.path.join(VENDOR, "nanopb", name))
    return True


if __name__ == "__main__":
    print(f"vendored native sources -> {VENDOR}" if vendor()
          else "siblings absent/incomplete; using pre-vendored _native_build/")
