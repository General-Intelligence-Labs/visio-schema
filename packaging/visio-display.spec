# PyInstaller spec — builds the `visio-display` onedir bundle (incl. the --serve launcher).
#
#   pyinstaller packaging/visio-display.spec --noconfirm
#
# Build ON each target OS — PyInstaller can't cross-compile, and foxglove-sdk is a
# native (Rust/pyo3) extension. Output: dist/visio-display/ (a folder to zip + ship).
#
# NOTE: hidden-imports for native/plugin packages (foxglove, zeroconf, aiohttp) are
# collected below; if a frozen run hits a ModuleNotFoundError, extend the collect_all
# / hiddenimports lists — this is the usual first-CI-run tuning for PyInstaller.
import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all

import visio_schema

_pkg = os.path.dirname(visio_schema.__file__)
_pyroot = os.path.dirname(_pkg)   # the python/ import root

# Package data the launcher loads at runtime via Path(__file__): the selector page
# assets and the Foxglove starter layout.
datas = [
    (os.path.join(_pkg, "display", "static"), "visio_schema/display/static"),
    (os.path.join(_pkg, "display", "ego_layout.json"), "visio_schema/display"),
]
binaries = []
# The generated protobuf `_pb2` modules are imported dynamically (importlib) by
# visio_schema.wire.schema._load_payload_modules, and the proto subpackages (v1,
# foxglove) are PEP 420 namespace dirs (no __init__.py) that collect_submodules
# won't traverse — so PyInstaller's static analysis misses them entirely. Enumerate
# every .py under the package on disk and add each as a hidden import.
hiddenimports = ["serial.tools.list_ports", "ifaddr"]
for _py in glob.glob(os.path.join(_pkg, "**", "*.py"), recursive=True):
    _mod = os.path.relpath(_py, _pyroot)[:-3].replace(os.sep, ".")
    if _mod.endswith(".__init__"):
        _mod = _mod[: -len(".__init__")]
    hiddenimports.append(_mod)

# Pull in everything these ship — foxglove-sdk carries a native lib; zeroconf/aiohttp
# have submodules + C-extensions PyInstaller's static analysis can miss.
for _mod in ("foxglove", "zeroconf", "aiohttp"):
    _d, _b, _h = collect_all(_mod)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    [os.path.join(SPECPATH, "visio_launcher.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # The --serve launcher only uses the Foxglove sink; drop the Rerun/PyAV sink deps
    # (a large native viewer binary + ffmpeg) so the bundle stays lean.
    excludes=["rerun", "rerun_sdk", "av", "matplotlib", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="visio-display",
    console=False,   # no terminal window — double-clicking launches the browser UI
)
coll = COLLECT(exe, a.binaries, a.datas, name="visio-display")

# macOS: wrap the onedir in a clickable `.app` bundle — Finder can't double-click a
# bare Unix executable. (Windows gets a windowed `.exe` inside the onedir; Linux the
# onedir binary.)
if sys.platform == "darwin":
    app = BUNDLE(coll, name="visio-display.app",
                 bundle_identifier="dev.gilabs.visio-display")
