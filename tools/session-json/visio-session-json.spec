# PyInstaller spec — one self-contained `visio-session-json` executable.
#
#   pyinstaller tools/session-json/visio-session-json.spec --noconfirm
#   → dist/visio-session-json      (.exe on Windows)
#
# Onefile, deliberately: the whole tool is one stdlib-only script, so the result is a
# single ~10 MB file an operator can drop on a desktop, drag a folder onto, and mail
# on — nothing to unzip and no folder to keep together. Build ON each target OS;
# PyInstaller can't cross-compile.
#
# It shares nothing with packaging/visio-display.spec on purpose: that bundle carries
# foxglove-sdk, PyAV and aiohttp for the launcher, none of which this needs.
import os

a = Analysis(
    [os.path.join(SPECPATH, "session_json.py")],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    # The script imports nothing outside the stdlib. Dropping the heavy packages a
    # dev machine happens to have installed keeps the build honest and small.
    excludes=["visio_schema", "foxglove", "av", "aiohttp", "zeroconf", "serial",
              "rerun", "rerun_sdk", "numpy", "matplotlib", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="visio-session-json",
    console=True,   # a CLI — and the window a double-click opens is where it reports
)
