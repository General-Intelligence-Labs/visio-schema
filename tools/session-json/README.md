# `visio-session-json` — rebuild a recording's `session.json`

Every recording session used to carry a small `session.json` next to its `.mcap`
parts. Firmware now writes that capture metadata **inside** the MCAP instead (the
`visio.capture` metadata record), so a file copied off the card is self-contained —
and the sidecar is gone.

This puts it back, for pipelines that still read it. The output is **byte-for-byte**
the file the device used to write: same keys, same order, same number formatting.

```
recordings/
└── session_00005-1785067199/
    ├── ego_0000.mcap      ──▶  session.json      (rebuilt from the metadata inside)
    ├── ego_0001.mcap
    └── session.json
```

## Windows — no Python needed

Download `visio-session-json.exe` from the
[latest release](https://github.com/General-Intelligence-Labs/visio-schema/releases).
One file, nothing to install. Any of these work:

- **Drag a folder onto the .exe** — your card, one session, or a whole archive tree.
- **Double-click it** inside a recordings folder — rebuilds everything below it.
- **Run it** from `cmd` / PowerShell: `visio-session-json.exe D:\recordings`

It prints one line per session and holds the window open when you double-click or
drag onto it:

```
D:\recordings\session_00005-1785067199: rebuilt from ego_0000.mcap
D:\recordings\session_00006-1785069002: kept the existing session.json (--force to overwrite)
rebuilt 1, kept 1, failed 0
```

macOS and Linux builds are attached to the same release; `chmod +x` and run it the
same way.

## With Python (any 3.10+, no dependencies)

`session_json.py` is a single stdlib-only file — copy it anywhere and run it:

```bash
python session_json.py ~/recordings
```

## Options

| | |
|---|---|
| `PATH ...` | A session folder, a folder of sessions (searched all the way down), or a single `.mcap`. Defaults to the current folder. |
| `--force` | Overwrite an existing `session.json`. Without it, an existing file is left alone. |
| `--dry-run` | Report what would be rebuilt; write nothing. |
| `--stdout` | Print the JSON instead of writing sidecars. |

Exit status is `0` when nothing failed, `1` otherwise — so it can gate a batch job.

## What it can't do

**Recordings older than the in-MCAP metadata can't be rebuilt** — there is nothing
in the file to rebuild from. Those sessions were written by firmware that still
emitted the sidecar, so they already have it, and the tool says so rather than
inventing a blank one:

```
D:\old\session_00000: no capture metadata in any .mcap part — recorded by firmware
older than the in-MCAP metadata, whose sessions still have their sidecar
```

Fields the device didn't record (no GPS fix, no task typed in) come back as the same
blanks the sidecar always carried: `""`, `0`, `0.0000000`.

Fields that came *later* than the frozen layout — `operator_id`, `environment_id` —
are written at the end, so a session from before they existed rebuilds to the old
file plus two blank keys. Everything ahead of them is unchanged.

**GPS coordinates lose their last digit.** The sidecar wrote latitude and longitude
with 7 decimals; the firmware embeds them in the MCAP with 6. A session recorded at
`37.7698784` rebuilds as `37.7698780` — about 1 cm, and it is destroyed before this
tool sees the file, so no version of it can recover the digit. Every other field is
exact.

**A session whose part is torn past its identity keys is refused, not guessed.** If a
part's metadata record is damaged the tool moves on to the session's other parts (each
one re-emits it); if none survive, it reports the failure rather than writing a sidecar
with a blank device and a 1970 timestamp.

## The mapping

| `session.json` | `visio.capture` record |
|---|---|
| `device_id` | `serial` — the one field that changed name |
| everything else | the same key |

## Building the executable

One build per OS — PyInstaller can't cross-compile. CI
(`.github/workflows/session-json.yml`) does all three and attaches them to each
`v*` release; locally:

```bash
pip install pyinstaller
pyinstaller tools/session-json/visio-session-json.spec --noconfirm
# → dist/visio-session-json[.exe]
```

The builds are **not code-signed**, so Windows SmartScreen warns on first run
(*More info ▸ Run anyway*) and macOS Gatekeeper wants right-click ▸ *Open*.

Tests live with the rest of the Python suite:
[`python/tests/test_session_json.py`](../../python/tests/test_session_json.py) (`make pytest`).
