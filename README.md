# visio-schema

The **wire contract** for the Visio sensor ecosystem: the protobuf message definitions, the
byte-level framing codec, and the protocol specs that a Visio device, the `visio` bus, and any
third-party client all share. This repo is the single source of truth for *what Visio data looks
like on the wire* — it is not a transport, bus, or recording stack (those live in `visio`).

It ships as one Python package, `visio-schema`, that bundles the generated protobuf bindings next
to a small, hand-written framing codec, so you can read a live device or a recording with one
`pip install`.

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

The quickest way to see your data is the ready-to-run viewer below; to build your own tooling, jump
to [Write your own code](#write-your-own-code).

## Install

```bash
pip install "visio-schema[mcap] @ git+https://github.com/General-Intelligence-Labs/visio-schema@visio-schema-v0.2.0#subdirectory=python"
```

Released tags ship the generated bindings, so this needs no codegen toolchain. The `[mcap]` extra
adds MCAP read/write; omit it if you only need the live wire codec. For a development checkout
(building bindings from source, the optional native reader, troubleshooting), see
**[docs/install.md](docs/install.md)**.

## Quickstart — view a device or recording (no code)

The ready-to-run viewer **[`examples/python/visio_display.py`](examples/python/visio_display.py)**
streams a live device (or replays a recording) to a viewer — no code required. We recommend
**[Foxglove Studio](https://foxglove.dev)**: it has rich panels for video, IMU plots, and 3D, and
opens both live connections and recorded `.mcap` files.

### Set up Foxglove

Use either:

- **Desktop app** — download from [foxglove.dev/download](https://foxglove.dev/download); no account
  needed to open local files or local connections, or
- **Web app** — sign in at [app.foxglove.dev](https://app.foxglove.dev) (free account).

### View a live device

From a repo checkout, with the package installed (see [docs/install.md](docs/install.md)) and the
example deps:

```bash
pip install -r examples/python/requirements.txt

# stream the device to Foxglove (prints a ws:// URL to open)
python examples/python/visio_display.py --serial /dev/ttyACM0 --foxglove

# stream to Foxglove AND record an MCAP at the same time
python examples/python/visio_display.py --serial /dev/ttyACM0 --foxglove --out run.mcap
```

In Foxglove, choose **Open connection → Foxglove WebSocket** and enter the printed URL
(`ws://localhost:8765`). Import
[`examples/python/ego_layout.json`](examples/python/ego_layout.json) (**Layouts ▸ Import from
file**) for a ready-made panel set.

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

This repo vendors third-party code under permissive licenses: [MCAP](third_party/mcap/) (MIT),
and [nanopb](third_party/nanopb/) (zlib) and the [Foxglove SDK](third_party/foxglove-sdk/) (MIT)
as git submodules — each retains its own license.
