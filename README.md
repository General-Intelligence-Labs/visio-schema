# visio-schema

The **wire contract** for the Visio sensor ecosystem: the protobuf message definitions, the
byte-level framing codec, and the protocol specs that a Visio device, the Visio bus, and any
third-party client all share. This repo is the single source of truth for *what Visio data looks
like on the wire* — it is not a transport, bus, or recording stack (those live in a separate layer).

It ships as one Python package, `visio-schema`, that bundles the generated protobuf bindings next
to a small, hand-written framing codec — plus a ready-to-run **device launcher** you can download
and run with no Python at all (below).

## What you can do with it

A Visio device (e.g. a head-worn egocentric camera rig) streams camera, IMU, and encoder data as
COBS-framed protobuf over USB serial, and records the same data to [MCAP](https://mcap.dev) files.
With this package you can:

1. **Live-view a device** — stream camera, IMU, and encoder data to
   [Foxglove Studio](https://foxglove.dev), or record it to MCAP.
2. **Read a recording** — replay an `.mcap` file as decoded messages, or open it directly in
   Foxglove Studio.
3. **Integrate + command** — write your own client that reads streams *and* sends control commands
   back to the device (start/stop recording, calibrate, configure Wi-Fi/storage, …).

**Just want to see a device in Foxglove?** Use the download-and-run launcher below — no Python. To
build your own tooling, install the [Python library](#python-library) instead.

## View a device in Foxglove Studio — download & run (no install)

Download the ready-to-run **`visio-display` launcher** for your OS from the
[v0.2.1 release](https://github.com/General-Intelligence-Labs/visio-schema/releases/tag/v0.2.1),
unzip it, and **double-click** it — **macOS** `visio-display.app`, **Windows**
`visio-display\visio-display.exe`, or **Linux** `visio-display/visio-display`.

A page opens in your browser — pick your connected device (over **USB**, **Wi-Fi**, or a **manual AP
address**) and it streams live into **[Foxglove Studio](https://foxglove.dev)**. The launcher opens
Foxglove for you: the **desktop app** (works offline — install it from
[foxglove.dev/download](https://foxglove.dev/download) first) or a **browser tab** as a fallback. No
Python, no setup, no code — the page's **Quit** button stops it.

## Python library

Install a released build straight from the GitHub release with pip — grab the newest sdist URL from
the [releases page](https://github.com/General-Intelligence-Labs/visio-schema/releases):

```bash
pip install https://github.com/General-Intelligence-Labs/visio-schema/releases/download/v0.2.1/visio_schema-0.2.1.tar.gz
```

This includes MCAP read/write (`read_mcap` / `McapWriter`), the `visio-display` CLI viewer (next
section), and an optional native reader for higher throughput (identical pure-Python fallback
otherwise). For a development checkout building the bindings from source, see
**[docs/install.md](docs/install.md)**.

## CLI viewer — `visio-display`

Installed with the [Python library](#python-library), the **`visio-display`** command streams a live
device (or replays a recording) from the command line — the launcher's `--serve` above is the
friendly front-end to it. We recommend
**[Foxglove Studio](https://foxglove.dev)**: it has rich panels for video, IMU plots, and 3D, and
opens both live connections and recorded `.mcap` files.

### Set up Foxglove

Use either:

- **Desktop app** — download from [foxglove.dev/download](https://foxglove.dev/download); no account
  needed to open local files or local connections, or
- **Web app** — sign in at [app.foxglove.dev](https://app.foxglove.dev) (free account).

### View a live device

```bash
# stream the device to Foxglove (prints a ws:// URL to open)
visio-display --serial /dev/ttyACM0 --foxglove

# stream to Foxglove AND record an MCAP at the same time
visio-display --serial /dev/ttyACM0 --foxglove --out run.mcap
```

In Foxglove, choose **Open connection → Foxglove WebSocket** and enter the printed URL
(`ws://localhost:8765`). For a ready-made panel set, import the starter layout — `visio-display`
prints its absolute path alongside the connection URL — via **Layouts ▸ Import from file**.

### View a recording

Record one with `--out run.mcap` (above), or generate a sample without any hardware:

```bash
python examples/python/make_sample_mcap.py sample.mcap
```

Then open the `.mcap` **directly in Foxglove** (**File ▸ Open local file**) — no server needed. See
[`examples/README.md`](examples/README.md) for the full tour.

> **Rerun fallback.** Prefer a lightweight local 3D viewer with no account or browser? Pass
> `--rerun` instead of `--foxglove` to spawn the [Rerun](https://rerun.io) viewer (it auto-creates
> views): `--serial /dev/ttyACM0 --rerun`, or `--mcap-in run.mcap --rerun` to replay a file.

## Write your own code

For custom integrations — your own viewer, a recorder, or a client that sends commands back to the
device — the package exposes a small stable API. Every row is a `(message, channel)`:

```python
from visio_schema import read_serial

for msg, channel in read_serial("/dev/ttyACM0"):
    print(channel.topic, msg.seq, len(msg.payload))
```

Runnable recipes for all three use cases (live view, read a recording, integrate + send commands)
are in **[docs/usage.md](docs/usage.md)**. Everything you import from the package root
(`from visio_schema import …`) is the stable public API — see **[AGENTS.md](AGENTS.md)** for the
complete surface and the stability guarantee.

## Layout

| Path | Contents |
|---|---|
| [`proto/`](proto/) | The protobuf schema (`visio_schema.v1.*`) — the source of truth for message types. |
| [`python/`](python/) | The `visio-schema` Python package: the framing codec, the optional native reader, and the in-package generated bindings. |
| [`cpp/`](cpp/) | The C++ codec + nanopb bindings for embedded/on-device use. |
| [`docs/`](docs/) | User guides ([install](docs/install.md), [usage](docs/usage.md)) and the [protocol reference](docs/protocol/). |
| [`examples/`](examples/) | Runnable Python and C++ demos. |

## Documentation

- **[docs/install.md](docs/install.md)** — install and build, including from source and the optional native reader.
- **[docs/usage.md](docs/usage.md)** — the three use cases, end to end.
- **[docs/protocol/](docs/protocol/)** — the normative wire spec (framing, streams, timesync, Foxglove compat, versioning).
- **[AGENTS.md](AGENTS.md)** — the pinned public API and contributor guide (also read by coding agents).

## License

[MIT](LICENSE) © 2026 General Intelligence Labs.

This repo pulls third-party code under permissive licenses as git submodules:
[MCAP](third_party/mcap/) (MIT), [nanopb](third_party/nanopb/) (zlib), and the
[Foxglove SDK](third_party/foxglove-sdk/) (MIT) — each retains its own license.
