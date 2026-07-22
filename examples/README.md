# visio-schema examples

Minimal, dependency-light demos of the Visio wire. They use only the
`visio-schema` package (generated bindings + framing codec) plus a couple of
thin libraries. They are intentionally small — the heavier, bus-integrated
machinery lives in a separate bus/transport layer.

First make the package importable (from the repo root). For a release install or full
details, see [../docs/install.md](../docs/install.md):

```bash
make gen                       # generate the protobuf bindings in-tree (needs buf; see install.md)
pip install -e python          # or: make wheel && pip install dist/visio_schema-*.whl
```

## Python — live serial → Foxglove Studio, Rerun, and/or MCAP

The viewer ships in the package as the **`visio-display`** command (source:
`visio_schema/display/`). It reads Visio messages from a **live serial port** (or TCP, or an MCAP
file) and fans them out to any combination of a live Foxglove Studio WebSocket
server, a live **Rerun** viewer, and an MCAP recording. (To view an MCAP *file*,
open it in Studio directly; see below.)

```bash
pip install visio-schema

# live serial -> Rerun (spawns the viewer; auto-lays-out views)
visio-display --serial /dev/ttyACM0 --rerun

# live serial -> Foxglove Studio (the command prints a URL to open)
visio-display --serial /dev/ttyACM0 --foxglove

# live serial -> record an MCAP while watching live
visio-display --serial /dev/ttyACM0 --out run.mcap --rerun
```

`--rerun` spawns the **Rerun viewer** and logs each stream under its topic path.
Rerun auto-creates views, so there is **no manual panel setup**: IMU orientation
shows as a box rotated by the quaternion in a 3D view, and accel/gyro/quat appear
as scalar plots. (Needs `rerun-sdk`; `av` decodes the H.265 camera streams.)

`--foxglove` starts a **WebSocket data-source server** (not itself a viewer)
and prints a URL. Open it, or in **Foxglove Studio** choose **Open connection →
Foxglove WebSocket → `ws://localhost:8765`**. For a ready-made panel set, import
the starter layout — `visio-display` prints its absolute path alongside the URL —
via **Layouts ▸ Import from file**, or add panels by hand. Note: a bare IMU
quaternion has no built-in Foxglove renderer — the command also publishes a `/tf`
`FrameTransform` derived from it, so the **3D panel** (display frame `world`)
shows the orientation.

### No hardware? Generate a sample MCAP and open it

`python/make_sample_mcap.py` writes one multi-topic synthetic recording —
bundled raw IMU, a rotating fused orientation, and (if `av` is installed) an
H.264 video stream — using the canonical `visio_schema.McapWriter`:

```bash
python python/make_sample_mcap.py sample.mcap        # 5s clip (--seconds N to change)
```

Then **open the file directly in Foxglove Studio: File ▸ Open local file**.
No server, no `--foxglove` — file playback is Studio's job, not this script's.
Add a **Plot** panel for `/glove_left/imu_raw/3`, a **3D**/orientation panel for
`/glove_left/imu_quat/3`, and an **Image** panel for `/ego/video_compressed/0`.

> These scripts use the canonical `visio_schema.mcap.McapWriter` (+
> `ChannelRegistry` for live serial). The bus-integrated recorder is
> `visio_schema.mcap.McapWriterEndpoint` (the C++ `visio_schema::mcap::McapWriterEndpoint`)
> — a thin Endpoint adapter over the same `McapWriter`; attach it to a `Bus` as a
> sink. Its replay counterpart, `visio_schema.mcap.McapReaderEndpoint`, streams a
> recording in place of a live link so downstream is unchanged.

> H.264 video uses the `av` package, which `visio-schema` installs by default; in
> an environment without it, the sample is written IMU-only.

> **Protobuf schema naming.** These channels use protobuf encoding, so each
> channel's schema *name* is the protobuf full name (e.g.
> `visio_schema.v1.ros.geometry_msgs.Quaternion`) — that's how Foxglove
> resolves the type from the embedded `FileDescriptorSet`. The ROS-name remap
> documented in `docs/protocol/foxglove_compat.md` (`geometry_msgs/msg/Quaternion`)
> applies only to **ros2msg-encoded** channels; using it as a protobuf schema
> name makes Studio report *"no such type"*. Native ROS-panel matching for
> these types would require emitting a `ros2msg` schema, which these minimal
> examples do not.

Foxglove Studio can also open the written `.mcap` directly (File ▸ Open).

## Python — sync your clock to a device (heartbeat NTP)

`python/timesync_client.py` is the whole timesync exchange in one file: it answers the
device's heartbeat beacons, beacons back on a timer, closes the NTP loop, filters the
offset by lowest RTT, and shifts each inbound `Header.timestamp` onto your own clock —
which is what makes device data comparable with your machine's, or with a second device.

```bash
python python/timesync_client.py /dev/ttyACM0        # prints offset, RTT, and message age
```

The how-to it accompanies is [../docs/timesync_client.md](../docs/timesync_client.md); the
normative algorithm is [../docs/protocol/timesync.md](../docs/protocol/timesync.md).

## C++ — minimal embedded serial consumer

`cpp/serial_consumer.cc` reads COBS-framed core frames from a serial port,
decodes each Header, and prints it — the shape a Linux-class embedded board
(e.g. an RV1106 gripper) would use. It links the single `visio_schema` CMake
target.

```bash
cmake -S cpp -B cpp/build && cmake --build cpp/build
./cpp/build/serial_consumer /dev/ttyUSB0
```

Quick loopback test without hardware:

```bash
socat -d -d pty,raw,echo=0 pty,raw,echo=0     # prints two /dev/pts/N paths
./cpp/build/serial_consumer /dev/pts/A         # then feed frames into /dev/pts/B,
                                               # e.g. with the Python example's
                                               # serial reader pointed the other way
```
