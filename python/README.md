# visio-schema

The **wire contract** for the Visio sensor ecosystem: the generated protobuf
message bindings plus a small, hand-written framing codec, packaged so you can
read a live device or a recording with one `pip install`. It is the single
source of truth for *what Visio data looks like on the wire*; the transport,
bus, and recording stack live in a separate layer.

Imported as `visio_schema`. Full docs, the wire spec, and C++ sources are in the
[GitHub repository](https://github.com/General-Intelligence-Labs/visio-schema).

## Install

```bash
pip install visio-schema
```

One install includes MCAP read/write (`read_mcap` / `McapWriter`) and the
`visio-display` live viewer — no extras to choose. Released wheels (Linux
`manylinux_2_28` x86_64, macOS `universal2`, CPython 3.10–3.13) bundle an optional
native reader for higher throughput. If no wheel matches your platform, the sdist
installs a pure-Python reader with identical behavior — only throughput differs.

## Quickstart

```python
from visio_schema import read_serial, read_mcap, message_class

# live device -> (Message, Channel) rows
for msg, ch in read_serial("/dev/ttyACM0"):
    cls = message_class(ch.schema_name)        # resolve the payload type
    payload = cls.FromString(msg.payload)
    print(ch.topic, payload)

# replay a recording
for msg, ch in read_mcap("run.mcap"):
    ...
```

Send commands and read replies with `serial_endpoint(...)` + `command_message`.
See [usage.md](https://github.com/General-Intelligence-Labs/visio-schema/blob/main/docs/usage.md)
for the three end-to-end recipes.

## `visio-display` viewer

The package installs a `visio-display` console command that reads a live device
(serial or TCP) or replays an MCAP, and fans it out to a live
[Foxglove](https://foxglove.dev) WebSocket, a live [Rerun](https://rerun.io)
viewer, and/or an MCAP recording:

```bash
visio-display --serial /dev/ttyACM0 --rerun
visio-display --tcp my-device.local --foxglove
visio-display --mcap-in run.mcap --rerun
# also runnable as: python -m visio_schema.display
```

## License

MIT — see [LICENSE](https://github.com/General-Intelligence-Labs/visio-schema/blob/main/LICENSE).
