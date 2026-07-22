# AGENTS.md — visio-schema

Guidance for coding agents (and humans) working in this repo. `CLAUDE.md` is a symlink to this
file. For end-user docs see [README.md](README.md), [docs/install.md](docs/install.md), and
[docs/usage.md](docs/usage.md); for the wire spec see [docs/protocol/](docs/protocol/).

## What this is

`visio-schema` is the **wire contract** for the Visio sensor ecosystem: the protobuf message
definitions (`proto/`), a small hand-written framing codec, and the protocol specs. It is *not* a
transport, bus, or recording stack — those live in a separate bus/transport layer. It ships as one Python
package (`visio-schema` → `import visio_schema`) and a C++ codec (`cpp/`).

## Import model

- **Stable public API: the package root.** `from visio_schema import <name>` — see the table below.
- **The schema: generated proto packages.** `visio_schema.v1.*` and `visio_schema.foxglove.*` (e.g.
  `from visio_schema.v1.control import command_pb2`). The message types live here.
- **Everything else is advanced/internal.** The submodules (`visio_schema.transport`,
  `visio_schema.wire.codec`, `visio_schema.routing`, the concrete endpoint classes, the fd helpers,
  the registry internals) are importable but **not covered by the stability guarantee** and may
  change without a major bump. Prefer the facade; reach into submodules only for bus/transport work.

`visio_schema` is a regular package whose generated subpackages (`v1`, `foxglove`, `wire`) are PEP
420 namespace dirs — do not assume every subdir has an `__init__.py`.

## Stable public API (pinned)

These twelve names re-exported from `visio_schema`, plus the generated proto schema, are the
contract downstream consumers depend on:

| Name | Purpose |
|---|---|
| `read_serial(port)` | Live device → iterator of resolved `(Message, Channel)` rows. The simple read path. |
| `serial_endpoint(path)` | Open a **bidirectional** live device → `Endpoint` (native reader, pure-Python fallback). |
| `Endpoint` | The connection: `.start(on_inbound, on_closed)`, `.send(msg)`, `.stop()`. |
| `Message` | A wire message: `stream_id`, `payload` (bytes), `seq`, `timestamp`. |
| `ChannelRegistry` | Learns `DeviceInfo` announces; `.resolved(msgs)` yields `(Message, Channel)` data rows. |
| `Channel` | A topic + its protobuf schema (the generated `DeviceInfo.Channel`). |
| `make_channel(topic, schema_name, *, stream_id)` | Build a self-describing `Channel` (fills the schema) to write. |
| `read_mcap(path)` | Replay an MCAP as `(Message, Channel)` pairs. *(needs the `mcap` extra)* |
| `McapWriter` | Write `(Message, Channel)` pairs to MCAP. *(needs the `mcap` extra)* |
| `message_class(schema_name)` | Resolve a schema name → generated message class (to decode payloads). |
| `command_message(cmd)` | Wrap a `command_pb2.Command` into a `Message` on the `COMMAND` stream. |
| `COMMAND` | The control `stream_id` commands ride on. |

Argument order in the Python API is `(Message, Channel)` everywhere — `read_serial`/`read_mcap`/
`resolved` yield it and `McapWriter.write(msg, channel)` takes it — so a read round-trips to a write
without reordering. (The C++ `McapWriter::Write(channel, msg)` keeps channel-first; the two
languages have separate idioms.)

Proto entry points consumers rely on: `v1.control.command_pb2` (`Command` + its oneof bodies +
`StartRecording`/`StopRecording`/`Identify`/`SetAutoStart`/`ConnectWifi`/`SetStorage`/
`ListRecordings`/`GetState`/`SetCalibration`/`SetAutoUpload`/`SetNoticeLang`/`SetResolution`/
`SetAudioRecording`), `v1.control.command_result_pb2.CommandResult`,
`v1.wire.header_pb2` (`Header`, `ControlStream`), `v1.service.device_info.device_info_pb2`
(`DeviceInfo`, `Channel`), `v1.sensor.*`, `v1.calibration.*`, `foxglove.*`.

### ⚠️ Stability contract — do not break this surface

Changing or removing a facade name (or a proto entry point above), or **silently widening** the
facade, is a breaking change requiring a MAJOR version bump per
[docs/protocol/versioning.md](docs/protocol/versioning.md). The surface is pinned by
[`python/tests/test_public_api.py`](python/tests/test_public_api.py) — it fails on any add, remove,
or rename. If a change to the public API is intentional, update **together**:

1. the facade `__all__` in `python/visio_schema/__init__.py`,
2. `FACADE_API` in `python/tests/test_public_api.py`,
3. this table,
4. `CHANGELOG.md`, and bump the version.

Internal submodule surfaces are deliberately *not* pinned — change them freely.

## The three use cases (entry points)

See [docs/usage.md](docs/usage.md) and [`examples/`](examples/) for full recipes.

1. **Live view** — `for msg, ch in read_serial("/dev/ttyACM0"):`. Full viewer: the `visio-display`
   command (installed with the package; source in `visio_schema/display/`).
2. **Read a recording** — `for msg, ch in read_mcap("run.mcap"):`; decode with
   `message_class(ch.schema_name)`; write with `McapWriter.write(msg, make_channel(...))`. Sample:
   `examples/python/make_sample_mcap.py`.
3. **Integrate + command** — `serial_endpoint(...).send(command_message(Command(...)))`; read the
   `CommandResult` reply in `on_inbound` (control streams are not yielded by `resolved()`). Embedded
   C++ reader: `examples/cpp/serial_consumer.cc`.

## Working in this repo

- Generated bindings are **source-only at HEAD** (gitignored) and vendored at release tags. After
  changing `proto/`, run `make gen` before importing/testing.
- Build/install: see [docs/install.md](docs/install.md). Quick: `git submodule update --init` →
  `npm install -g @bufbuild/buf` → `make gen` → `pip install -e "python[dev]"`.

Common commands:

```bash
make gen                                   # regenerate Python + C++ (nanopb) bindings
make pytest                                # Python suite
cd python && python -m pytest tests -q     # same, directly
VISIO_NO_NATIVE=1 python -m pytest tests -q  # force the pure-Python reader
make cpp                                    # C++ codec tests
python -m ruff check .                      # lint (config in python/pyproject.toml)
make lint && make breaking                  # buf proto lint + wire-compat check
```

- Run `ruff` on Python you touch; keep the codec changes tied to the golden vectors in
  `python/tests` / `cpp/tests` and to [docs/protocol/framing.md](docs/protocol/framing.md).
- Never commit secrets. Do not commit generated `_pb2` files (they're gitignored at HEAD).
