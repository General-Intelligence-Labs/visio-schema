#!/usr/bin/env python3
"""Generate a sample multi-topic visio MCAP so you can try things without hardware.

Writes one MCAP containing several synthetic streams at once, using the same
`McapSink` the live example ships:

  - STREAM_IMU_RAW    bundled raw gyro/accel        -> /glove_left/imu_raw/3
  - STREAM_IMU_QUAT   slowly rotating orientation   -> /glove_left/imu_quat/3
  - STREAM_VIDEO_COMPRESSED  H.264 moving colour bar -> /ego/video_compressed/0
                      (only if PyAV is installed: `pip install av`)

Then just open the file in Foxglove Studio — **File ▸ Open local file** — and
add panels: a Plot for the IMU fields, an Image panel for the video. (The live
visio_foxglove.py script is for real serial streams, not file playback.)

    make gen && pip install -e python         # make the package importable
    pip install -r examples/python/requirements.txt
    python make_sample_mcap.py sample.mcap    # default: sample.mcap, 5 s
"""
from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from pathlib import Path

from visio_schema.wire.message import Message
from visio_schema.wire.streams import message_class
from visio_schema.wire.v1.header_pb2 import DeviceClass, StreamKind

# Reuse the McapSink shipped with the live example (sibling file, not a package).
_EXAMPLE = Path(__file__).resolve().parent / "visio_foxglove.py"
_spec = importlib.util.spec_from_file_location("visio_foxglove", _EXAMPLE)
_ex = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ex)

START_NS = 1_700_000_000 * 1_000_000_000      # fixed epoch -> reproducible file

# IMU (on a glove)
GLOVE = DeviceClass.DEVICE_GLOVE_LEFT
IMU_INDEX = 3                                  # one finger's IMU
RAW_HZ, BUNDLE_HZ, QUAT_HZ = 200, 20, 50

# Video (on an egocentric rig)
EGO = DeviceClass.DEVICE_EGO
CAM_INDEX = 0
VID_W, VID_H, VID_FPS = 320, 240, 30


# --------------------------------------------------------------------------- #
# IMU streams                                                                   #
# --------------------------------------------------------------------------- #
def _imu_raw_bundle(t0_ns: int, n_samples: int, spin: float) -> bytes:
    m = message_class("visio_schema.sensor.v1.ImuRaw")()
    m.first_sample_time.FromNanoseconds(t0_ns)
    dt = 1_000_000_000 // RAW_HZ
    for i in range(n_samples):
        s = m.samples.add()
        s.t_offset_ns = i * dt
        s.angular_velocity.z = spin            # rad/s
        s.linear_acceleration.z = 9.81         # gravity
    return m.SerializeToString()


def _imu_quat(angle_rad: float) -> bytes:
    q = message_class("visio_schema.ros.geometry_msgs.v1.Quaternion")()
    q.w = math.cos(angle_rad / 2)
    q.z = math.sin(angle_rad / 2)              # rotation about +Z
    return q.SerializeToString()


def _gen_imu(sink, seconds: float) -> int:
    n = 0
    bundle_dt = 1_000_000_000 // BUNDLE_HZ
    samples_per_bundle = RAW_HZ // BUNDLE_HZ
    for k in range(int(seconds * BUNDLE_HZ)):
        t = START_NS + k * bundle_dt
        spin = 0.5 * math.sin(2 * math.pi * 0.2 * k / BUNDLE_HZ)   # gentle sway
        msg = Message(
            stream=StreamKind.STREAM_IMU_RAW, stream_index=IMU_INDEX,
            payload=_imu_raw_bundle(t, samples_per_bundle, spin), device=GLOVE, seq=k,
        )
        msg.timestamp.FromNanoseconds(t)
        sink.write(msg)
        n += 1

    quat_dt = 1_000_000_000 // QUAT_HZ
    total = int(seconds * QUAT_HZ)
    for k in range(total):
        t = START_NS + k * quat_dt
        msg = Message(
            stream=StreamKind.STREAM_IMU_QUAT, stream_index=IMU_INDEX,
            payload=_imu_quat(2 * math.pi * k / total), device=GLOVE, seq=k,
        )
        msg.timestamp.FromNanoseconds(t)
        sink.write(msg)
        n += 1
    return n


# --------------------------------------------------------------------------- #
# Video stream (optional — needs PyAV)                                          #
# --------------------------------------------------------------------------- #
def _encode_h264(seconds: float):
    """Yield (frame_index, annexb_bytes) for each encoded H.264 access unit.

    The `h264` raw-Annex-B muxer keeps SPS/PPS in-band; a small GOP (`g`) means
    frequent keyframes so a late-joining viewer recovers quickly.
    """
    import av
    import numpy as np

    container = av.open("/dev/null", mode="w", format="h264")
    stream = container.add_stream("libx264", rate=VID_FPS)
    stream.width, stream.height = VID_W, VID_H
    stream.pix_fmt = "yuv420p"
    stream.options = {"tune": "zerolatency", "g": "15", "bf": "0"}

    n_frames = int(seconds * VID_FPS)
    idx = 0
    for i in range(n_frames):
        arr = np.zeros((VID_H, VID_W, 3), dtype=np.uint8)
        x = int((i / max(n_frames - 1, 1)) * (VID_W - 40))
        arr[:, x : x + 40] = (255, 128, 0)                         # moving bar
        arr[VID_H // 2 - 2 : VID_H // 2 + 2, :] = (0, 128, 255)    # horizon
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for packet in stream.encode(frame):
            yield idx, bytes(packet)
            idx += 1
    for packet in stream.encode(None):                             # flush
        yield idx, bytes(packet)
        idx += 1
    container.close()


def _video_payload(data: bytes, ts_ns: int) -> bytes:
    msg = message_class("foxglove.CompressedVideo")()
    msg.timestamp.FromNanoseconds(ts_ns)
    msg.frame_id = "ego_cam"
    msg.format = "h264"
    msg.data = data
    return msg.SerializeToString()


def _gen_video(sink, seconds: float) -> int:
    frame_dt = 1_000_000_000 // VID_FPS
    n = 0
    for idx, annexb in _encode_h264(seconds):
        t = START_NS + idx * frame_dt
        msg = Message(
            stream=StreamKind.STREAM_VIDEO_COMPRESSED, stream_index=CAM_INDEX,
            payload=_video_payload(annexb, t), device=EGO, seq=idx,
        )
        msg.timestamp.FromNanoseconds(t)
        sink.write(msg)
        n += 1
    return n


# --------------------------------------------------------------------------- #
def generate(path: str, seconds: float) -> dict[str, int]:
    sink = _ex.McapSink(path)
    counts = {"imu": _gen_imu(sink, seconds)}
    try:
        counts["video"] = _gen_video(sink, seconds)
    except ImportError:
        counts["video"] = 0  # PyAV not installed; IMU-only file
    sink.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", nargs="?", default="sample.mcap", help="output .mcap path")
    p.add_argument("--seconds", type=float, default=5.0, help="clip length (default 5)")
    args = p.parse_args(argv)

    counts = generate(args.out, args.seconds)
    total = sum(counts.values())
    print(f"wrote {total} messages to {args.out} ({args.seconds:g}s): "
          f"{counts['imu']} imu" +
          (f", {counts['video']} video" if counts["video"]
           else " (no video — `pip install av` to include H.264)"),
          file=sys.stderr)
    print(f"open {args.out} in Foxglove Studio: File ▸ Open local file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
