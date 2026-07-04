# Packaging the `visio-display` launcher

Builds a self-contained, **clickable** bundle of `visio-display` (including the
`--serve` device-picker launcher) so operators can run it without installing Python
— **macOS `visio-display.app`**, **Windows `visio-display.exe`** (windowed, inside
the onedir), **Linux** onedir binary. One build per OS — PyInstaller can't
cross-compile, and `foxglove-sdk` is a native extension.

> **Windows note.** `visio_schema`'s transport has a Windows read path (pyserial
> high-level read + socket `recv`) alongside the POSIX fd path, so the package runs
> on Windows — but the Windows launcher is **build-validated in CI, not yet
> runtime-tested on Windows hardware**.

## Build locally

From the `visio-schema` repo root, with the package installed
(`pip install ./python`) and its `_pb2` generated (`make gen`):

```bash
pip install pyinstaller
pyinstaller packaging/visio-display.spec --noconfirm
# → dist/visio-display/  (and dist/visio-display.app on macOS)
dist/visio-display/visio-display          # no args → the launcher (same as --serve)
```

CI (`.github/workflows/launcher.yml`) does this across `ubuntu-latest`, `macos-14`,
and `windows-latest`, and on a `v*` release tag attaches the per-OS zips to the
GitHub release alongside the wheels/sdist.

## Running the bundle

**Double-click** it — `visio-display.app` (macOS), `visio-display\visio-display.exe`
(Windows), or `visio-display/visio-display` (Linux). It opens the launcher page in
your browser; pick a device (USB / Wi-Fi / manual AP host) and it opens Foxglove —
the desktop app via a `foxglove://` deep link (offline-capable; **install Foxglove
Studio** first), or a browser tab as a fallback. The page's **Quit** button stops it.

## Unsigned builds — first-run bypass

The v1 zips are **not code-signed or notarized**, so the OS warns on first run:

- **macOS** (Gatekeeper): right-click → *Open* the first time, or
  `xattr -dr com.apple.quarantine visio-display.app`.
- **Windows** (SmartScreen): *More info* → *Run anyway*.

Signing/notarization is a planned follow-up (an Apple Developer ID + a Windows
code-signing cert as CI secrets).

## Notes

- The bundle **excludes** the Rerun/PyAV sink (`--rerun`) to stay small; it's the
  launcher distribution. For the full CLI, install the package (see the repo
  [README](../README.md#python-library)).
- If a frozen run hits a `ModuleNotFoundError`, extend the `collect_all` /
  `hiddenimports` lists in `visio-display.spec` — the usual PyInstaller tuning.
