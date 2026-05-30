# visio-schema examples

Minimal, dependency-light demos of the Visio wire. They use only the
`visio-schema` package (generated bindings + framing codec) plus a couple of
thin libraries. They are intentionally small — the heavier, bus-integrated
machinery lives in `visio-mq`.

First make the package importable (from the repo root):

```bash
make gen                       # generate bindings into gen/
pip install -e python          # or: make wheel && pip install dist/visio_schema-*.whl
```

## Python — serial / MCAP → MCAP / Foxglove Studio

`python/visio_foxglove.py` reads Visio messages from a live serial port or a
visio-written MCAP file, and writes them to an MCAP file and/or a live
Foxglove Studio WebSocket server.

```bash
pip install -r python/requirements.txt

# live serial -> Foxglove Studio (then connect Studio to ws://localhost:8765)
python python/visio_foxglove.py --serial /dev/ttyUSB0 --foxglove

# live serial -> record an MCAP while watching live
python python/visio_foxglove.py --serial /dev/ttyUSB0 --out run.mcap --foxglove

# replay a recorded MCAP into Foxglove Studio
python python/visio_foxglove.py --mcap run.mcap --foxglove
```

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
