"""Shared test builders: synthesize self-describing Visio MCAPs.

Raw ``mcap.writer.Writer`` + ``visio_schema`` protos: a fixture has to write
shapes a well-behaved writer would refuse (missing schema records, odd chunk
seams, a bare ``visio.capture`` record), so it stays below
`McapWriter`. Video is **all-intra by default, no
B-frames** (one independent packet per frame), so a dropped packet drops exactly
that frame — the clean gap-preserving case — and decoded frame content encodes
its index for stamp-integrity checks.

``add_camera(..., keyint=N)`` builds a real GOP instead, which is what a keyframe
test needs: under the default every frame IS a keyframe, so a sampler that reads
the bitstream and one that ignores it are indistinguishable.
"""

from __future__ import annotations

import numpy as np
from mcap.writer import Writer
from scipy.spatial.transform import Rotation

from visio_schema import make_channel, message_class

FRAME_DT = 33_000_000  # ns (~30 fps)
T0 = 1_700_000_000 * 1_000_000_000

VIDEO = "foxglove.CompressedVideo"
IMAGE = "foxglove.CompressedImage"
IMU_RAW = "visio_schema.v1.sensor.ImuRaw"
CAM_CALIB = "foxglove.CameraCalibration"
FRAME_TF = "foxglove.FrameTransform"
IMU_CALIB = "visio_schema.v1.calibration.ImuCalibration"
FRAME_INFO = "visio_schema.v1.sensor.CameraFrameInfo"
QUAT = "visio_schema.v1.ros.geometry_msgs.Quaternion"
POSE_IN_FRAME = "foxglove.PoseInFrame"
JOINT_STATES = "foxglove.JointStates"

# Plausible ego-like fisheye intrinsics (not a real rig).
CAM_K = [[460.0, 0, 320.0], [0, 460.0, 240.0], [0, 0, 1.0]]
CAM_D = [0.01, -0.002, 0.0003, -0.0001]
CAM_W, CAM_H = 640, 480


def indexed_frames(n, h=64, w=96):
    """n RGB frames whose red channel ramps per frame (content == index)."""
    out = []
    for i in range(n):
        f = np.zeros((h, w, 3), dtype=np.uint8)
        f[:, :, 0] = 20 + i * 10
        out.append(f)
    return out


def frame_index_of(img):
    """Recover the frame index from a decoded (gray or RGB) indexed frame."""
    red = img[..., 0] if img.ndim == 3 else img
    return int(round((float(np.median(red)) - 20) / 10))


def encode_h265(frames, *, keyint=1):
    """Encode RGB frames to one H.265 packet each (in order), IDR every ``keyint``.

    Delegates to the SDK's canonical ``HevcEncoder`` so the load-bearing x265 params
    live in one place. ``keyint=1`` (the default) is all-intra: every frame is
    independently droppable, which is what the gap-preserving tests rely on.
    """
    from visio_schema.reader import HevcEncoder

    enc = HevcEncoder(frames[0].shape[1], frames[0].shape[0], keyint=keyint)
    packets = [au for f in frames for _, au in enc.encode(f, 0)]
    return packets + [au for _, au in enc.flush()]


def encode_jpeg(rgb, *, quality=90, max_side=0):
    """RGB -> JPEG bytes, optionally long-edge capped. Never upscales.

    One owner, for the same reason `encode_h265` delegates to the SDK's encoder:
    three copies of this had already drifted apart on whether they guard the
    upscale case.
    """
    import cv2

    h, w = rgb.shape[:2]
    if max_side and max(h, w) > max_side:
        s = max_side / max(h, w)
        rgb = cv2.resize(rgb, (max(1, round(w * s)), max(1, round(h * s))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes(), (rgb.shape[1], rgb.shape[0])


class RecBuilder:
    """Accumulate channels + messages, then write a self-describing MCAP."""

    def __init__(self, path, device=None, capture=None):
        self.path = path
        self.device = device
        self.capture = capture
        self._rows = []  # (topic, schema_name, t_ns, payload_bytes)

    def _topic(self, topic):
        return f"/{self.device}{topic}" if self.device else topic

    def add_camera(self, topic, frames, *, t0=T0, dt=FRAME_DT, skew=0, drop=(),
                   keyint=1, fmt="h265"):
        """`fmt` only relabels the packets — it does not re-encode.

        Enough to exercise a reader's codec dispatch (e.g. that `is_keyframe`
        refuses a format it has no parser for) without needing an encoder for it.
        """
        packets = encode_h265(frames, keyint=keyint)
        for i, pkt in enumerate(packets):
            if i in drop:
                continue
            t = t0 + i * dt + skew
            m = message_class(VIDEO)()
            m.timestamp.FromNanoseconds(t)
            m.frame_id = topic.rsplit("/", 1)[-1]
            m.format = fmt
            m.data = pkt
            self._rows.append((self._topic(topic), VIDEO, t, m.SerializeToString()))
        return self

    def add_compressed_image(self, topic, frames, *, t0=T0, dt=FRAME_DT, quality=90,
                             frame_id=None, fmt="jpeg", max_side=0):
        """Self-contained JPEG stills — what `stages/sample-frames` writes.

        A separate builder from `add_camera` because it is a different schema with
        a different contract: every message decodes on its own, so there is no GOP,
        no `keyint`, and no `drop` semantics to preserve. `max_side` applies the
        same long-edge cap the real stage does, so a fixture can be built at the
        sampled resolution rather than resized by hand.
        """
        for i, img in enumerate(frames):
            data, _wh = encode_jpeg(img, quality=quality, max_side=max_side)
            t = t0 + i * dt
            m = message_class(IMAGE)()
            m.timestamp.FromNanoseconds(t)
            m.frame_id = (topic.rsplit("/", 1)[-1] if frame_id is None else frame_id)
            m.format = fmt
            m.data = data
            self._rows.append((self._topic(topic), IMAGE, t, m.SerializeToString()))
        return self

    def add_frame_info(self, cam_topic, *, n, t0=T0, dt=FRAME_DT, exposures=None,
                       drop=(), hts=612, vts=2432, pclk=44.65):
        """`frame_info` entries on the camera's sibling topic, one per frame.

        `exposures` is per-index seconds (default a flat 4.002 ms, which is what a
        real static-scene ego capture carries); `drop` omits indices to model the
        producer dropping an unbindable entry or the ISP losing one. Defaults are
        the AR0234's measured HTS/VTS/pclk, so line time works out to 13.706 us.
        """
        for i in range(n):
            if i in drop:
                continue
            t = t0 + i * dt
            m = message_class(FRAME_INFO)()
            m.timestamp.FromNanoseconds(t)
            m.isp_frame_id = i
            m.exposure_time_s = (exposures[i] if exposures is not None
                                 else 0.004002192988991737)
            m.analog_gain = 4.515625
            m.digital_gain = 1.0
            m.isp_digital_gain = 1.0
            m.iso = 0
            m.coarse_integration_time_lines = 292
            m.line_length_pixels = hts
            m.frame_length_lines = vts
            m.pixel_clock_mhz = pclk
            self._rows.append((self._topic(cam_topic + "/frame_info"), FRAME_INFO,
                               t, m.SerializeToString()))
        return self

    def add_imu_bundle(self, topic, t_ns, offsets, *, gyro=(0, 0, 0.1),
                       accel=(0, 0, 9.81)):
        m = message_class(IMU_RAW)()
        m.first_sample_time.FromNanoseconds(t_ns)
        for off in offsets:
            s = m.samples.add()
            s.t_offset_ns = off
            s.angular_velocity.x, s.angular_velocity.y, s.angular_velocity.z = gyro
            (s.linear_acceleration.x, s.linear_acceleration.y,
             s.linear_acceleration.z) = accel
        self._rows.append((self._topic(topic), IMU_RAW, t_ns, m.SerializeToString()))
        return self

    def add_quat(self, topic, *, n, t0=T0, dt=FRAME_DT, quat=(0.0, 0.0, 0.0, 1.0)):
        """The BNO's fused orientation — a topic outside the decodable set.

        Useful for two things: it is what the Z-up gravity alignment reads, and
        because a bare `stream()` never surfaces it, it makes "raw mode selects
        every topic" an observable claim rather than an assertion about counts.
        """
        for i in range(n):
            m = message_class(QUAT)()
            m.x, m.y, m.z, m.w = (float(v) for v in quat)
            t = t0 + i * dt
            m.timestamp.FromNanoseconds(t)
            self._rows.append((self._topic(topic), QUAT, t, m.SerializeToString()))
        return self

    def add_poses(self, topic, *, n, t0=T0, dt=FRAME_DT, step=(0.01, 0.0, 0.0),
                  yaw_step_deg=1.0, frame_id="odom"):
        """A robot/VIO pose track: position ramps, yaw rotates a fixed step.

        Both quantities advance LINEARLY in the index so an interpolated sample at
        a known fraction has a closed-form expected value — an interpolator that
        merely returns a neighbour is otherwise indistinguishable from one that
        blends, at any sample rate.
        """
        for i in range(n):
            m = message_class(POSE_IN_FRAME)()
            t = t0 + i * dt
            m.timestamp.FromNanoseconds(t)
            m.frame_id = frame_id
            m.pose.position.x = i * step[0]
            m.pose.position.y = i * step[1]
            m.pose.position.z = i * step[2]
            q = Rotation.from_euler("z", i * yaw_step_deg, degrees=True).as_quat()
            (m.pose.orientation.x, m.pose.orientation.y,
             m.pose.orientation.z, m.pose.orientation.w) = (float(v) for v in q)
            self._rows.append(
                (self._topic(topic), POSE_IN_FRAME, t, m.SerializeToString()))
        return self

    def add_joint_states(self, topic, *, n, t0=T0, dt=FRAME_DT,
                         joints=("left", "right"), start=0.0, step=0.05,
                         with_velocity=False):
        """A gripper/joint track. `position` only by default: it is `optional` on
        the wire, and telling an absent field from a real 0.0 is the point of the
        adapter, so a fixture that always sets everything cannot show it."""
        for i in range(n):
            m = message_class(JOINT_STATES)()
            t = t0 + i * dt
            m.timestamp.FromNanoseconds(t)
            for k, name in enumerate(joints):
                j = m.joints.add()
                j.name = name
                j.position = start + (i * step) + k
                if with_velocity:
                    j.velocity = float(i)
            self._rows.append(
                (self._topic(topic), JOINT_STATES, t, m.SerializeToString()))
        return self

    def add_camera_calib(self, topic, *, K=CAM_K, D=CAM_D, w=CAM_W, h=CAM_H,
                         model="kannala_brandt"):
        m = message_class(CAM_CALIB)()
        m.timestamp.FromNanoseconds(T0)
        m.frame_id = topic.split("/")[-2]
        m.width, m.height = w, h
        m.distortion_model = model
        m.K.extend(np.asarray(K, float).reshape(-1).tolist())
        m.D.extend(list(D))
        self._rows.append((self._topic(topic), CAM_CALIB, T0, m.SerializeToString()))
        return self

    def add_extrinsics(self, topic, *, T, quat=(0, 0, 0, 1), child="cam1"):
        m = message_class(FRAME_TF)()
        m.timestamp.FromNanoseconds(T0)
        m.parent_frame_id = "cam0"
        m.child_frame_id = child
        m.translation.x, m.translation.y, m.translation.z = T
        m.rotation.x, m.rotation.y, m.rotation.z, m.rotation.w = quat
        self._rows.append((self._topic(topic), FRAME_TF, T0, m.SerializeToString()))
        return self

    def add_imu_calib(self, topic, *, dt_s=0.039, rate=200.0, an=0.002, gn=0.0002):
        m = message_class(IMU_CALIB)()
        m.time_offset_to_cam0_s = dt_s
        m.update_rate_hz = rate
        m.accel_noise_density = an
        m.gyro_noise_density = gn
        self._rows.append((self._topic(topic), IMU_CALIB, T0, m.SerializeToString()))
        return self

    def write(self, chunk_size=None, sort=True):
        """`chunk_size` (bytes) forces multiple chunks — a truncation test needs a
        cut that falls between chunks of one file, not just before the footer.

        `sort=False` writes rows in the order they were added, so FILE order and
        log-time order differ. Every other fixture sorts, which makes a scan's
        first/last bounds indistinguishable from min/max."""
        if sort:
            self._rows.sort(key=lambda r: r[2])  # log_time order
        with self.path.open("wb") as fh:
            w = Writer(fh) if chunk_size is None else Writer(fh, chunk_size=chunk_size)
            w.start(profile="visio", library="visio_schema.reader test")
            if self.capture:
                w.add_metadata("visio.capture", self.capture)
            schema_ids, chan_ids, seqs = {}, {}, {}
            for topic, schema_name, t, payload in self._rows:
                if schema_name not in schema_ids:
                    ch = make_channel(topic, schema_name, stream_id=0)
                    schema_ids[schema_name] = w.register_schema(
                        name=schema_name, encoding="protobuf", data=ch.schema
                    )
                if topic not in chan_ids:
                    chan_ids[topic] = w.register_channel(
                        topic=topic, message_encoding="protobuf",
                        schema_id=schema_ids[schema_name],
                    )
                    seqs[topic] = 0
                w.add_message(
                    channel_id=chan_ids[topic], log_time=t, publish_time=t,
                    sequence=seqs[topic], data=payload,
                )
                seqs[topic] += 1
            w.finish()
        return self.path


# ~178 deg about x, then a few degrees of yaw and pitch — asymmetric on purpose.
IMU_QUAT = tuple(
    Rotation.from_euler("xyz", [178.0, 3.0, -4.0], degrees=True).as_quat()
)
STEREO_QUAT = tuple(
    Rotation.from_euler("xyz", [0.6, -1.1, 0.4], degrees=True).as_quat()
)


def stereo_calib_builder(path):
    """A builder pre-loaded with stereo cameras' full calibration."""
    b = RecBuilder(path)
    b.add_camera_calib("/ego/camera/0/intrinsics")
    b.add_camera_calib("/ego/camera/1/intrinsics")
    b.add_extrinsics("/ego/camera/1/extrinsics", T=(-0.062, 0, 0),
                 quat=STEREO_QUAT, child="cam1")
    # cam0 <- imu0, published verbatim as kalibr's T_cam_imu. Near-inverted like the
    # real mount, but deliberately ASYMMETRIC: an exactly-180-deg rotation is its own
    # transpose, so it cannot detect a transposed rotation block.
    b.add_extrinsics("/ego/imu/0/extrinsics", T=(-0.018, 0.026, -0.002),
                     quat=IMU_QUAT, child="imu0")
    b.add_imu_calib("/ego/imu/0/info")
    return b


def sidecar_messages(path):
    """Every message a sidecar holds, as ``(topic, log_time, bytes)`` in file order.

    The comparison a batching test needs: two runs that differ only in batch size
    must produce byte-identical sequences, and ORDER is half the claim — so this
    deliberately returns a list, not a set.
    """
    from mcap.reader import make_reader

    with path.open("rb") as fh:
        return [(ch.topic, m.log_time, m.data)
                for _s, ch, m in make_reader(fh).iter_messages()]


class Accumulating:
    """Mixin that makes a test fake hold `batch` items before answering.

    A fake that answers in step with its input cannot exercise a submit/flush
    loop at all: every stage test would pass even if the stage dropped `flush()`
    or reordered results. Subclasses provide the synchronous single-item method
    and call `_hold(result, meta)` from `submit`.

    `seen` records the size of each released group, so a test can assert the
    batch shapes directly instead of inferring them from output counts.
    """

    def __init__(self, batch: int = 1) -> None:
        self.batch = batch
        self.seen: list[int] = []
        self._pending: list[tuple] = []

    def _hold(self, result, meta):
        self._pending.append((result, meta))
        return self.flush() if len(self._pending) >= self.batch else []

    def flush(self):
        if not self._pending:
            return []
        self.seen.append(len(self._pending))
        out, self._pending = self._pending, []
        return out


def unindexed_mcap(path):
    """A VALID mcap with no summary section — what a writer that never closed
    cleanly leaves behind. Truncating a good file does NOT produce this; it
    produces a corrupt record, which the reader rejects earlier and for a
    different reason."""
    from mcap.writer import IndexType, Writer

    with open(path, "wb") as f:
        w = Writer(
            f,
            index_types=IndexType.NONE,
            repeat_schemas=False,
            repeat_channels=False,
            use_statistics=False,
            use_summary_offsets=False,
        )
        w.start()
        sid = w.register_schema(
            name="foxglove.PoseInFrame", encoding="protobuf", data=b""
        )
        cid = w.register_channel(
            topic="/pose/x", message_encoding="protobuf", schema_id=sid
        )
        w.add_message(channel_id=cid, log_time=1, data=b"", publish_time=1)
        w.finish()
    return path
