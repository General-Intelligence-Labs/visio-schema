# visio-schema examples

Minimal, dependency-light demos of the Visio wire. They use only the
`visio-schema` package (generated bindings + framing codec) plus a couple of
thin libraries. They are intentionally small — the heavier, bus-integrated
machinery lives in `visio`.

First make the package importable (from the repo root):

```bash
make gen                       # generate bindings into gen/
pip install -e python          # or: make wheel && pip install dist/visio_schema-*.whl
```

## Python — live serial → Foxglove Studio, Rerun, and/or MCAP

`python/visio_display.py` reads Visio messages from a **live serial port** and
fans them out to any combination of a live Foxglove Studio WebSocket server, a
live **Rerun** viewer, and an MCAP recording. (It only handles live streams — to
view an MCAP *file*, open it in Studio directly; see below.)

```bash
pip install -r python/requirements.txt

# live serial -> Rerun (spawns the viewer; auto-lays-out views)
python python/visio_display.py --serial /dev/ttyACM0 --rerun

# live serial -> Foxglove Studio (the script prints a URL to open)
python python/visio_display.py --serial /dev/ttyACM0 --foxglove

# live serial -> record an MCAP while watching live
python python/visio_display.py --serial /dev/ttyACM0 --out run.mcap --rerun
```

`--rerun` spawns the **Rerun viewer** and logs each stream under its topic path.
Rerun auto-creates views, so there is **no manual panel setup**: IMU orientation
shows as a box rotated by the quaternion in a 3D view, and accel/gyro/quat appear
as scalar plots. (Needs `rerun-sdk`; `av` decodes the H.265 camera streams.)

`--foxglove` starts a **WebSocket data-source server** (not itself a viewer)
and prints a URL. Open it, or in **Foxglove Studio** choose **Open connection →
Foxglove WebSocket → `ws://localhost:8765`**. Import `python/visio_layout.json`
(**Layouts ▸ Import from file**) for a ready-made panel set, or add panels by
hand. Note: a bare IMU quaternion has no built-in Foxglove renderer — the script
also publishes a `/tf` `FrameTransform` derived from it, so the **3D panel**
(display frame `world`) shows the orientation.

### No hardware? Generate a sample MCAP and open it

`python/make_sample_mcap.py` writes one multi-topic synthetic recording —
bundled raw IMU, a rotating fused orientation, and (if `av` is installed) an
H.264 video stream — using the same `McapSink`:

```bash
python python/make_sample_mcap.py sample.mcap        # 5s clip (--seconds N to change)
```

Then **open the file directly in Foxglove Studio: File ▸ Open local file**.
No server, no `--foxglove` — file playback is Studio's job, not this script's.
Add a **Plot** panel for `/glove_left/imu_raw/3`, a **3D**/orientation panel for
`/glove_left/imu_quat/3`, and an **Image** panel for `/ego/video_compressed/0`.

> These scripts use a small standalone `McapSink` so they depend only on
> `visio-schema`. The bus-integrated, embeddable recorder is `McapEndpoint` in
> the **visio** package (`visio.mq.endpoints.mcap` / the C++
> `visio::mq::McapEndpoint`) — attach it to a `Bus` as a sink. See
> `visio/README.md`.

> H.264 video needs the `av` package (`pip install av`); without it the sample
> is written IMU-only.

> **Protobuf schema naming.** These channels use protobuf encoding, so each
> channel's schema *name* is the protobuf full name (e.g.
> `visio_schema.ros.geometry_msgs.v1.Quaternion`) — that's how Foxglove
> resolves the type from the embedded `FileDescriptorSet`. The ROS-name remap
> documented in `docs/foxglove_compat.md` (`geometry_msgs/msg/Quaternion`)
> applies only to **ros2msg-encoded** channels; using it as a protobuf schema
> name makes Studio report *"no such type"*. Native ROS-panel matching for
> these types would require emitting a `ros2msg` schema, which these minimal
> examples do not.

Foxglove Studio can also open the written `.mcap` directly (File ▸ Open).

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
