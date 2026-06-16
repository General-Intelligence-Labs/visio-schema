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
| `protobuf>=5.26`, `cobs>=1.2` | always (installed automatically) |
| `mcap>=1.1` | only to read/write MCAP recordings — the `[mcap]` extra |
| A C/C++ compiler | only for the **optional** native reader; without one you get the pure-Python reader |
| `git` + submodules, Node/`npm` | only for a **from-source** checkout (to run protobuf codegen) |

The native reader (`_creader`) is a faster, GIL-free serial reader. It is entirely optional: if it
can't be built or loaded, the package transparently falls back to the pure-Python reader. Behavior
is identical; only throughput differs.

## Option A — install a release (recommended)

Released tags ship the generated protobuf bindings, so no codegen toolchain is required.

```bash
# the wire codec only
pip install "visio-schema @ git+https://github.com/General-Intelligence-Labs/visio-schema@visio-schema-v0.2.0#subdirectory=python"

# with MCAP read/write
pip install "visio-schema[mcap] @ git+https://github.com/General-Intelligence-Labs/visio-schema@visio-schema-v0.2.0#subdirectory=python"
```

A plain `git+…` install does not initialize submodules, so the native reader is skipped and you get
the pure-Python reader — correct, just slower. To get the compiled native reader, install a
prebuilt wheel (from PyPI or a GitHub release once published — these are built per Python version
for Linux and macOS), or build from source (Option B).

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
`make cpp` (C++ codec tests), `make wheel` (build a distributable wheel), `make clean`.

## Forcing the pure-Python reader

Set `VISIO_NO_NATIVE=1` to ignore the native extension even when it is installed — useful for
debugging or to confirm parity:

```bash
VISIO_NO_NATIVE=1 python -m pytest tests -q
```

## Troubleshooting

- **`ImportError: MCAP support needs the 'mcap' package`** — install the extra: `pip install
  visio-schema[mcap]`. You only hit this if you actually call `read_mcap` / `McapWriter`; importing
  the package never requires `mcap`.
- **`buf: command not found` during `make gen`** — install it (`npm install -g @bufbuild/buf`) or
  add `node_modules/.bin` to your `PATH`.
- **`make gen` errors about missing protos / nanopb** — initialize the submodules:
  `git submodule update --init third_party/nanopb third_party/foxglove-sdk third_party/mcap`.
- **Native reader didn't build** — that's fine; the pure-Python reader is used automatically. To
  build it you need a C/C++ compiler; check the `pip install` output for the compiler error.
- **Permission denied opening `/dev/ttyACM0`** — add your user to the `dialout` group
  (`sudo usermod -aG dialout $USER`, then re-login) or run with sufficient privileges.
