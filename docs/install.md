# Installing visio-schema

The package is `visio-schema`; you import it as `visio_schema`. There are two ways to get it: from
a **release** (no build tools) or from **source** (a development checkout). Most users want the
first.

> Using a coding agent? The commands below are copy-pasteable and self-contained — point your agent
> at this file and the [AGENTS.md](../AGENTS.md) contract and it can set up the environment and
> wire up the three use cases in [usage.md](usage.md) for you.

## Requirements

| | Needed for |
|---|---|
| Python ≥ 3.10 | always |
| `protobuf`, `cobs`, `mcap`, `pyserial`, `foxglove-sdk`, `rerun-sdk`, `av` | always (installed automatically) — the codec, MCAP read/write, and the `visio-display` viewer |
| A C/C++ compiler | only for the **optional** native reader; without one you get the pure-Python reader |
| `git` + submodules, Node/`npm` | only for a **from-source** checkout (to run protobuf codegen) |

The native reader (`_creader`) is a faster, GIL-free serial reader. It is entirely optional: if it
can't be built or loaded, the package transparently falls back to the pure-Python reader. Behavior
is identical; only throughput differs.

## Option A — install a release (recommended)

Install a released **sdist** straight from the GitHub release with pip — grab the newest URL from
the [releases page](https://github.com/General-Intelligence-Labs/visio-schema/releases):

```bash
pip install https://github.com/General-Intelligence-Labs/visio-schema/releases/download/v0.3.0/visio_schema-0.3.0.tar.gz
```

This includes the wire codec + generated bindings, MCAP read/write, and the `visio-display` viewer —
no extras to choose. The sdist ships the generated bindings (no codegen toolchain required) and
installs the pure-Python reader; it additionally builds the optional native reader for higher
throughput when a C/C++ compiler is present. (Release wheels that pre-bundle the native reader for
Linux `manylinux_2_28` x86_64 and macOS `universal2`, CPython 3.10–3.13, are attached to releases as
they are published.)

The package installs the **`visio-display`** command (also runnable as
`python -m visio_schema.display`):

```bash
visio-display --serial /dev/ttyACM0 --rerun     # live device -> Rerun
visio-display --tcp my-device.local --foxglove  # live device -> Foxglove WebSocket
visio-display --mcap-in run.mcap --rerun        # replay a recording
```

### Install from a git tag (before a PyPI release)

Release tags also ship the generated bindings, so a direct `git+…` install needs no codegen
toolchain:

```bash
pip install "visio-schema @ git+https://github.com/General-Intelligence-Labs/visio-schema@v0.3.0#subdirectory=python"
```

A plain `git+…` install does not initialize submodules, so the native reader is skipped and you get
the pure-Python reader — correct, just slower. For the compiled native reader, install a released
wheel from PyPI, or build from source (Option B).

Verify:

```bash
python -c "from visio_schema import serial_endpoint, read_mcap; print('ok')"
```

## Option B — from source (development)

Use this to change the schema, build the native reader, or run the test suite. Codegen uses
[`buf`](https://buf.build) (installed via npm) plus the vendored `nanopb` generator.

```bash
git clone https://github.com/General-Intelligence-Labs/visio-schema
cd visio-schema
git submodule update --init third_party/nanopb third_party/foxglove-sdk third_party/mcap

npm install -g @bufbuild/buf        # or: npm install && export PATH="$PATH:$(pwd)/node_modules/.bin"
make gen                            # generate Python + C++ (nanopb) bindings in-tree

pip install -e "python[dev]"        # editable install; builds the native reader if a compiler is present
```

Verify and test:

```bash
python -c "import visio_schema; print(sorted(visio_schema.__all__))"
cd python && python -m pytest tests -q
```

Useful `make` targets (see the `Makefile`): `make gen` (codegen), `make pytest` (Python suite),
`make cpp` (C++ codec tests), `make wheel` / `make sdist` / `make dist` (build distributables),
`make clean`. To cut a PyPI release, see [publishing.md](publishing.md).

## Forcing the pure-Python reader

Set `VISIO_NO_NATIVE=1` to ignore the native extension even when it is installed — useful for
debugging or to confirm parity:

```bash
VISIO_NO_NATIVE=1 python -m pytest tests -q
```

## Troubleshooting

- **`ImportError: MCAP support needs the 'mcap' package`** — `mcap` ships as a default dependency,
  so a normal install won't hit this; if you do, your environment is missing it: `pip install mcap`
  (or reinstall the package). `mcap` is imported lazily, so importing `visio_schema` itself never
  requires it.
- **`buf: command not found` during `make gen`** — install it (`npm install -g @bufbuild/buf`) or
  add `node_modules/.bin` to your `PATH`.
- **`make gen` errors about missing protos / nanopb** — initialize the submodules:
  `git submodule update --init third_party/nanopb third_party/foxglove-sdk third_party/mcap`.
- **Native reader didn't build** — that's fine; the pure-Python reader is used automatically. To
  build it you need a C/C++ compiler; check the `pip install` output for the compiler error.
- **Permission denied opening `/dev/ttyACM0`** — add your user to the `dialout` group
  (`sudo usermod -aG dialout $USER`, then re-login) or run with sufficient privileges.
