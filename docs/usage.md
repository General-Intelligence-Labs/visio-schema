# Using visio-schema

Three end-to-end recipes, one per use case. Every snippet imports from the package root
(`from visio_schema import …`) — the [stable public API](../AGENTS.md). Message types come from the
generated schema packages (`visio_schema.v1.*`, `visio_schema.foxglove.*`). Install first: see
[install.md](install.md).

Mental model: a Visio link carries a stream of **messages**, each tagged with a numeric
`stream_id`. The device periodically announces a **`DeviceInfo`** that maps each `stream_id` to a
**`Channel`** (a topic name + its protobuf schema). A `ChannelRegistry` learns those announces and
turns raw messages into `(message, channel)` data rows. A recording carries the same channels, so
live and replay feed your code identically.

---

## 1. Live-view a device

Open the serial port, let a `ChannelRegistry` resolve topics from the device's announces, and
`read_serial` opens the port, learns the device's topics from its `DeviceInfo` announces, and
yields a resolved `(message, channel)` row for each data message — the same shape `read_mcap`
yields, so live and replay code are identical. Stop by breaking out of the loop.

```python
from visio_schema import read_serial

for msg, channel in read_serial("/dev/ttyACM0"):   # native reader if available, else pure-Python
    print(f"{channel.topic:30s} seq={msg.seq} {len(msg.payload)} bytes")
```

To **decode** a payload, look up its class by the channel's schema name (see
[reading recordings](#2-read-a-recording) for the pattern — it's the same).

`read_serial` runs on the calling thread and is read-only. If you also need to **send** to the
device (use case 3), use `serial_endpoint` instead — it gives a bidirectional `Endpoint`.

For a complete viewer that fans the live stream out to **Foxglove Studio**, **Rerun**, and/or an
**MCAP recording** (with H.265 video decode and 3D transforms), use the `visio-display`
command (included with `pip install visio-schema`; source in `visio_schema.display`):

```bash
visio-display --serial /dev/ttyACM0 --rerun
visio-display --serial /dev/ttyACM0 --foxglove
visio-display --serial /dev/ttyACM0 --out run.mcap
```

### Recording while you watch

`McapWriter.write(message, channel)` takes the rows straight from `read_serial` (same order):

```python
from visio_schema import read_serial, McapWriter

with McapWriter("run.mcap") as rec:
    for msg, channel in read_serial("/dev/ttyACM0"):
        rec.write(msg, channel)
```

`McapWriter` uses the `mcap` package, which is installed by default with `visio-schema`.

---

## 2. Read a recording

`read_mcap` replays an `.mcap` file as the same `(message, channel)` rows a live stream produces.
Each channel is self-describing, so you can decode any payload by name with `message_class` — no
device or `DeviceInfo` required.

```python
from visio_schema import read_mcap, message_class

for msg, channel in read_mcap("run.mcap"):
    payload = message_class(channel.schema_name)()   # the generated proto class for this topic
    payload.ParseFromString(msg.payload)
    print(channel.topic, channel.schema_name, msg.seq)
    # e.g. for an ImuRaw topic: payload.linear_acceleration, payload.angular_velocity, …
```

To **view** a recording without writing code, open the `.mcap` directly in
[Foxglove Studio](https://foxglove.dev) — the embedded schemas let it resolve every topic.

No device handy? Generate a synthetic recording (bundled IMU + a rotating quaternion + an H.264
color-bar video) with [`examples/python/make_sample_mcap.py`](../examples/python/make_sample_mcap.py):

```bash
python examples/python/make_sample_mcap.py sample.mcap
```

To **write your own** recording from scratch, build a self-describing channel with `make_channel`
(it fills the schema from the type name) and hand `(message, channel)` to `McapWriter.write`:

```python
from visio_schema import McapWriter, Message, make_channel

ch = make_channel("/imu/0/raw", "visio_schema.v1.sensor.ImuRaw", stream_id=16)
with McapWriter("out.mcap") as w:
    w.write(Message(stream_id=ch.id, payload=imu.SerializeToString()), ch)
```

---

## 3. Integrate + send commands

A custom client reads streams *and* sends control commands back to the device. Sending needs the
bidirectional `serial_endpoint` (not the read-only `read_serial`): build a `command_pb2.Command`,
wrap it with `command_message`, and `send` it. The device replies with a `CommandResult` on the
`COMMAND` stream.

```python
from visio_schema import serial_endpoint, COMMAND, command_message
from visio_schema.v1.control import command_pb2, command_result_pb2

ep = serial_endpoint("/dev/ttyACM0")

def on_inbound(msg, _ep):
    if msg.stream_id == COMMAND:                       # a command reply
        result = command_result_pb2.CommandResult()
        result.ParseFromString(msg.payload)
        status = "ok" if result.ok else f"error: {result.error_message}"
        print(f"command {result.command_id}: {status}")

ep.start(on_inbound, None)

# Start a recording session on the device.
cmd = command_pb2.Command(
    target_device="ego",                              # the device name from its DeviceInfo announce
    command_id=1,                                     # your id, echoed back in the CommandResult
    start_recording=command_pb2.StartRecording(session_name="demo"),
)
ep.send(command_message(cmd))
```

> **Note:** command replies arrive in `on_inbound`, *not* through `ChannelRegistry.resolved()`.
> Control streams (like `COMMAND`) carry no `Channel`, so `resolved()` only yields data rows;
> filter on `msg.stream_id == COMMAND` to catch results.

Other commands set on the `Command` oneof: `stop_recording`, `identify`, `get_state`,
`list_recordings`, `set_auto_start`, `connect_wifi`, `scan_wifi`, `set_storage`, `test_storage`,
and `set_calibration` (camera intrinsics/extrinsics, IMU, encoder). See
[`proto/visio_schema/v1/control/command.proto`](../proto/visio_schema/v1/control/command.proto)
for every field.

For a **zero-dependency embedded** reader (COBS frames → topic, no protobuf runtime, cross-compiles
to the device), see [`examples/cpp/serial_consumer.cc`](../examples/cpp/serial_consumer.cc).

---

## Going lower-level

The framing codec, the concrete endpoint classes, the fd helpers, and the registry internals are
reachable through the submodules (`visio_schema.transport`, `visio_schema.wire.codec`,
`visio_schema.routing`). They are **advanced/internal** — useful for building a bus or a custom
transport, but not part of the [stability guarantee](../AGENTS.md). For the byte-level details, see
the [protocol reference](protocol/).
