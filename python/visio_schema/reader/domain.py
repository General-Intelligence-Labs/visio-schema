"""Domain types — the SDK's thin *processing* representations (alignment §8.1).

Reuse, don't rebuild: every wire type stays in ``visio_schema``
(``foxglove.*``, ``visio_schema.v1.*``). The types here model only the
*decoded* data the schema deliberately does not carry — decoded pixels, an
unbundled IMU sample, an ergonomic calibration view — and are constructed
*from* the schema protos. ``Frame``/``ImuSample``/``Calibration`` are the only
genuinely new types; ``SyncGroup``/``TopicInfo`` are SDK plumbing with no
schema analog.

**One clock.** ``t_ns`` is the recording's wire stamp — the heartbeat-synced
``Header.timestamp``, which is what the MCAP ``log_time`` holds — everywhere,
for every element kind. The SDK applies **no** timestamp correction on read.

That is deliberate. It used to apply the calibrated cam-IMU offset, which meant
elements carried two stamps (a corrected ``t_ns`` and a raw ``pts_ns``), and
every sidecar writer had to remember which one to use. The correction served
exactly one consumer — VIO — which estimates that offset online anyway from a
config seed, so pre-applying it only hid the baseline its estimate is relative
to. Sensor-latency and exposure models now belong to the consumer that needs
them; the SDK's job is to surface the *inputs* (``Calibration.cam_imu_dt_ns``,
and per-frame exposure) rather than to bake in one interpretation of them.

This module is the bottom layer: it imports nothing else in
``visio_schema.reader``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

Ns = int  # nanoseconds on the heartbeat-synchronized clock (int64)

# The wire schema names this reader knows by name. They sit on the floor, beside
# the element types, because BOTH `session` (which selects and parses them) and
# `adapters` (which turns them into elements) need them — and `adapters` sits
# below `session`, so it cannot reach up for them. `session` re-exports these, so
# `session` re-exports them, so a consumer importing them from there still works.
VIDEO_SCHEMA = "foxglove.CompressedVideo"
IMAGE_SCHEMA = "foxglove.CompressedImage"
IMU_RAW_SCHEMA = "visio_schema.v1.sensor.ImuRaw"
FRAME_INFO_SCHEMA = "visio_schema.v1.sensor.CameraFrameInfo"
CAM_CALIB_SCHEMA = "foxglove.CameraCalibration"
FRAME_TF_SCHEMA = "foxglove.FrameTransform"
IMU_CALIB_SCHEMA = "visio_schema.v1.calibration.ImuCalibration"
POSE_SCHEMA = "foxglove.PoseInFrame"
JOINT_STATES_SCHEMA = "foxglove.JointStates"



def make_T(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Rotation + translation -> a 4x4 rigid transform."""
    T = np.eye(4)
    T[:3, :3] = np.asarray(R, float).reshape(3, 3)
    T[:3, 3] = np.asarray(t, float).reshape(3)
    return T


@dataclass(frozen=True)
class FrameExposure:
    """What the ISP's AE had in effect for one frame (``CameraFrameInfo``).

    Attached to :class:`Frame` so a consumer can build its own exposure-midpoint or
    rolling-shutter model. The SDK deliberately builds **none** — the right model
    differs per consumer, and the sign of a midpoint term depends on a camera PTS
    convention that is not yet measured (``docs/alignment.md`` §9.2). This type is
    the *input*, not a correction.

    ``line_time_ns`` is derived (``line_length_pixels / pixel_clock_mhz``) because
    that is the form every consumer wants: the rolling-shutter delay between
    consecutive rows. It supersedes the never-populated
    ``Calibration.rs_line_delay_ns`` — it is per-frame and self-contained, so a clip
    needs no side-channel calibration to model row skew. It is ``None``, never 0.0,
    when the producer published no sensor timing: 0.0 is what a *global-shutter*
    sensor legitimately reports, so a consumer must be able to tell the two apart.
    """

    exposure_time_s: float
    # rolling-shutter per-row delay (~13.7 us on AR0234); None if unknown
    line_time_ns: float | None
    frame_length_lines: int  # VTS
    analog_gain: float
    digital_gain: float
    isp_digital_gain: float
    iso: int
    coarse_integration_time_lines: int
    # The producer's counter for the entry this came from — a diagnostic, never a
    # join key (visio-schema 0.7.0). None when this exposure was interpolated.
    isp_frame_id: int | None = None
    # True when no entry existed for this frame and the value was reconstructed
    # from its neighbours. NEVER left absent mid-stream: a frame with no exposure
    # would make a consumer's correction snap back to zero for that frame alone,
    # a step of up to T_exp/2 — exactly the jitter such a correction removes.
    interpolated: bool = False


@dataclass(frozen=True, eq=False)
class Frame:
    """A decoded camera frame stamped with its capture PTS.

    ``image`` is ``(H, W, 3)`` uint8 (RGB) or ``(H, W)`` uint8 (grayscale). It is
    a host ndarray on the CPU path, or a **device** array (cupy / anything with
    ``__cuda_array_interface__``) on the GPU path — this is pure data, ops never
    branch on it (the backend is chosen when the pipeline is built). ``event`` is
    an optional CUDA event a device consumer waits on before touching ``image``.

    ``exposure`` is ``None`` **only** when the recording carries no ``frame_info``
    stream at all (it is opt-in on the device and off by default), never because
    one frame's entry was missing — see :class:`FrameExposure`.
    """

    topic: str
    t_ns: Ns  # the wire stamp — see the module docstring: ONE clock
    image: np.ndarray  # host ndarray OR device (cupy/CAI) array
    frame_id: str = ""
    event: int | None = None  # CUDA event handle (device frames); None on CPU
    exposure: FrameExposure | None = None  # None => recording has no frame_info

    @property
    def is_gray(self) -> bool:
        return self.image.ndim == 2


@dataclass(frozen=True, eq=False)
class ImuSample:
    """One IMU sample, unbundled onto the recording's wire clock (§2)."""

    topic: str
    t_ns: Ns
    gyro: np.ndarray  # (3,) rad/s
    accel: np.ndarray  # (3,) m/s^2
    mag: np.ndarray | None = None  # (3,) uT, if present


@dataclass(frozen=True, eq=False)
class Record:
    """One decoded non-pixel message from the union — a SIDE read, not an element.

    The read counterpart of `SidecarWriter.write`: a derived topic (hand boxes, a
    pose, frame_info) comes back as its own protobuf message on the recording's
    ONE clock, so it joins a `Frame` by exact `t_ns` equality with nothing to
    convert.

    A first-class `Element`: naming a derived topic in `Session.stream` yields
    these interleaved with the frames, on the same clock and through the same
    reorder buffer, so `ops.sync` matches detections to their frame with the very
    mechanism that already pairs the stereo cameras. A sidecar reads back exactly
    like a native stream — the point of `docs/output.md`'s contract.

    Carries no pixels: derived topics are decoded, never interpreted, so a stage
    reads `msg` fields directly.

    `msg` is the GENERATED protobuf class where this process has the module, and
    one built from the file's OWN embedded descriptor set where it does not —
    field access is identical, `isinstance` is not. Read fields; never compare
    types.

    **Exactly one of `msg` / `data` is populated.** The decoded path parses and
    leaves `data` empty; `Session.stream(raw=True)` fills `data` with the wire
    payload and leaves `msg` None, so a passthrough consumer (stages/merge)
    writes bytes straight back with no parse and no re-serialize. One Element
    type rather than a second variant every consumer would have to learn — and
    `raw` is the only mode that produces the `msg is None` case, so nothing that
    named a topic today can meet it.
    """

    topic: str
    t_ns: Ns  # the wire stamp, same clock as Frame/ImuSample
    schema_name: str
    msg: object | None
    data: bytes = b""  # wire payload; populated by raw mode ONLY


@dataclass(frozen=True, eq=False)
class Pose:
    """A 6-DoF pose, parsed from ``foxglove.PoseInFrame``.

    Typed rather than left as a `Record` because a pose is the one element kind a
    consumer needs to *resample between* — and interpolation is dispatched on the
    element type (`interp`), so a pose arriving as an opaque `Record` can only ever
    be held or picked nearest. Position lerps; orientation slerps.

    ``orientation`` is ``(x, y, z, w)`` — foxglove's order and scipy's, so it
    round-trips through `build.pose_in_frame` with no reordering.
    """

    topic: str
    t_ns: Ns  # the wire stamp, same clock as Frame/ImuSample
    position: np.ndarray  # (3,) metres
    orientation: np.ndarray  # (4,) xyzw, unit
    frame_id: str = ""

    def pose7(self) -> np.ndarray:
        """``[x, y, z, qx, qy, qz, qw]`` — the flat row consumers pack."""
        return np.concatenate([self.position, self.orientation])


@dataclass(frozen=True, eq=False)
class JointState:
    """Named joint positions at one instant, from ``foxglove.JointStates``.

    Sparse by design: the proto's ``position``/``velocity``/``effort`` are
    `optional double`, so a joint that published none is ABSENT from these dicts
    rather than present as 0.0 — a real gripper width of 0.0 (fully closed) is
    otherwise indistinguishable from "the producer said nothing".

    That is also why interpolation runs per joint over the *intersection* of two
    samples: a joint present at one end of a bracket has nothing to blend against.
    """

    topic: str
    t_ns: Ns
    positions: dict[str, float]
    velocities: dict[str, float] | None = None
    efforts: dict[str, float] | None = None


@dataclass(frozen=True, eq=False)
class Tick:
    """A bare clock mark — an instant with no measurement attached.

    For a loop with NO image in it — teleop, an intervention controller, a
    recorder, a safety monitor. The loop pushes one of these at its control rate
    and they become the ``match`` key, so its own clock both defines the group
    instants and advances the watermark that releases them; a live stream then
    self-drives through the same `sync` call a recording uses, with no pull API.

    NOT the right grid for a vision policy. There the camera is the match key on
    both sides, because the training rows were built on the camera's instants and
    serving on a tick would align the policy's inputs differently from how they
    were baked — and would let a frame silently repeat when the loop runs faster
    than the camera.

    Deliberately NOT interpolable: a tick marks a time, it does not measure
    anything, so there is no value between two of them.
    """

    topic: str
    t_ns: Ns


# What the streaming pass yields — a closed union (isinstance-routable in
# `sync` passthrough). A new element kind (e.g. an AudioSample) extends this.
Element = Frame | ImuSample | Record | Pose | JointState | Tick


@dataclass(frozen=True, eq=False)
class CameraCalib:
    """One camera's intrinsics, parsed from ``foxglove.CameraCalibration``."""

    topic: str
    width: int
    height: int
    model: str  # distortion_model, e.g. "kannala_brandt" / "equidistant"
    K: np.ndarray  # (3, 3)
    D: np.ndarray  # (n,)
    frame_id: str = ""
    R: np.ndarray | None = None  # (3, 3) rectification, if present
    P: np.ndarray | None = None  # (3, 4) projection, if present


@dataclass(frozen=True, eq=False)
class Calibration:
    """Ergonomic aggregate over the recording's calibration messages.

    Built FROM ``foxglove.CameraCalibration`` + ``foxglove.FrameTransform`` +
    ``visio_schema.v1.calibration.ImuCalibration``, adding the one derived field
    the protos don't carry (``cam_imu_dt_ns``). The rolling-shutter line delay is
    NOT here: it lives on :class:`FrameExposure`, per frame and derived from the
    sensor timing the clip carries itself.
    """

    cams: dict[str, CameraCalib]  # keyed by camera topic
    # Stereo extrinsics: the POSE OF cam1 IN cam0 — `p_cam0 = stereo_R @ p_cam1 +
    # stereo_T` — exactly as carried on `/<dev>/camera/1/extrinsics`, which the
    # device publishes as `inv(T_c1_c0)` (visio-setup/calib/push.py:147). This is
    # the direction `cv2.fisheye.stereoRectify(left=cam1, right=cam0, R, T)` wants,
    # which `rectify.py` relies on.
    stereo_R: np.ndarray | None = None  # (3, 3)
    stereo_T: np.ndarray | None = None  # (3,) metres
    baseline_m: float | None = None
    # cam0 <- imu0: the 4x4 rigid transform taking a point in the IMU frame to the
    # cam0 frame (`p_cam0 = T_cam_imu @ p_imu0`). This IS kalibr's `cam0.T_cam_imu`
    # verbatim — the device publishes it unmodified on `/<dev>/imu/0/extrinsics`
    # (visio-setup calib/push.py:154), so it needs NO inversion. VIO consumes it
    # directly; `cam_imu_dt_ns` below is its temporal counterpart.
    T_cam_imu: np.ndarray | None = None  # (4, 4)
    cam_imu_dt_ns: int | None = None
    imu_rate_hz: float | None = None
    accel_noise_density: float | None = None
    gyro_noise_density: float | None = None


@dataclass(frozen=True)
class TopicInfo:
    """A topic present in the recording (from the MCAP summary, no full scan)."""

    topic: str
    schema_name: str
    message_count: int


@dataclass(frozen=True)
class KeyframeCadence:
    """How often one video topic carries an independently-decodable frame.

    What a caller divides its target sample interval by to get ``every_n`` for
    :meth:`Session.keyframe_stream`. **Measured, never assumed** — the GOP length
    is a producer setting, and a stream whose keyframes are further apart than the
    requested interval cannot be sampled at that interval at all, which is a thing
    the caller has to be able to see rather than discover as a wrong output rate.

    Measured identically on all 14 ego sessions in the office/kitchen corpora:
    ``frames_per_gop=30``, ``period_ns=1_002_000_000``. Note that 1002 ms is *not*
    1000 ms — an exactly-one-second sample grid does not divide it, which is why
    the caller rounds to an integer ``every_n`` and accepts the resulting rate
    rather than trying to hold a wall-clock grid the bitstream cannot supply.
    """

    topic: str
    period_ns: Ns  # median wall-clock time between keyframes
    frames_per_gop: int  # median access units between keyframes
    sampled: int  # keyframes the probe saw; >= 2, or there is no cadence to report

    def every_n_for(self, period_ns: Ns) -> tuple[int, Ns]:
        """A target interval -> ``(every_n, the interval that actually gives)``.

        Rounds rather than floors, so a target just under the GOP (1.000 s against
        the ego's 1.002 s) takes every keyframe instead of the 2 that flooring to
        zero-then-clamping would hide. The floor of 1 is the bitstream's own limit:
        nothing here samples faster than the keyframes arrive.

        Lives on the cadence, not at the call site, because the round-vs-floor
        question has one right answer and the second consumer would otherwise
        re-decide it.
        """
        every_n = max(1, round(period_ns / self.period_ns))
        return every_n, every_n * self.period_ns


@dataclass(frozen=True)
class SessionMeta:
    """Cheap session-level metadata (summary + ``visio.capture`` record)."""

    device: str | None
    start_ns: Ns | None
    end_ns: Ns | None
    capture: dict[str, str]  # the flat visio.capture record (may be empty)

    @property
    def duration_ns(self) -> Ns | None:
        if self.start_ns is None or self.end_ns is None:
            return None
        return self.end_ns - self.start_ns


@dataclass(frozen=True)
class FileSummary:
    """One input file, from its MCAP summary alone (no message scan).

    Purely descriptive — *what was read*, not *what it means*: ``metadata_names``
    lists the metadata-record names the file carries (e.g. ``visio.capture``,
    or a producer's own record) without interpreting any of them, so a consumer
    decides for itself which it cares about. ``status`` is ``ok`` for a file with
    an intact summary and ``truncated`` for one indexed by the tolerant linear
    scan (a tail-truncated recording); a file with no readable magic never
    reaches here — the reader refuses it outright.
    """

    path: str  # basename
    size: int  # bytes on disk
    status: Literal["ok", "truncated"]
    messages: int  # total across channels
    start_ns: Ns | None
    end_ns: Ns | None
    metadata_names: tuple[str, ...] | None  # None when scanned (truncated)


@dataclass(frozen=True)
class StreamSummary:
    """One input stream's files, in read order, plus which positional it was.

    ``origin`` is the stream's index into the ``Session`` constructor's
    positional streams, surviving the empty-stream filter — so a caller that
    knows its own ``open(recording, *sidecars)`` convention can label the streams
    (that labelling is the caller's business, not the reader's).
    """

    origin: int
    files: tuple[FileSummary, ...]


# How one key's value in a group was obtained. The distinction IS the point: a
# consumer that cannot tell an interpolated pose from a 400 ms-stale held one has
# no way to gate on it.
#
#   matched       a real element the sync matcher picked. Never blended — this is
#                 what a `match` key always yields, and the only honest answer for
#                 a decoded frame
#   exact         a resampled source sample landed exactly on the group time
#   interpolated  bracketed on both sides, blended by the type's interpolator
#   nearest       bracketed, but the type has no interpolator — or `mode="nearest"`
#                 was asked for. Also the head-of-stream case, where the only
#                 sample lies AFTER the group time: a large `residual_ns` marks it
#   held          no sample after the group time — the last one before it, carried
#                 forward. The only causal choice, so the low-latency default
#   extrapolated  no sample after the group time — projected from the last two
SampleMethod = Literal[
    "matched", "exact", "interpolated", "nearest", "held", "extrapolated"
]


@dataclass(frozen=True, eq=False)
class Sampled:
    """One key's value at the group time, and how it was obtained."""

    element: Element
    method: SampleMethod
    # Distance from the group time to the NEAREST contributing source stamp. Zero
    # on an exact hit; for an interpolated value it is the distance to the closer
    # bracket end, so it bounds the error the blend can hide; for a matched key it
    # is that member's own offset from the group time.
    residual_ns: Ns


@dataclass(frozen=True, eq=False)
class SyncGroup:
    """Every key's value at one instant — matched or resampled.

    The single output of `sync`, whichever way a key contributes. A stereo pair is
    *matched*: two real frames that fired together, picked, never blended. A wrist
    pose is *resampled*: evaluated at this group's instant, because it runs on its
    own asynchronous clock and no element of it exists at exactly this time. Both
    land here, each carrying `Sampled.method` so a consumer can tell which it got.

    One type rather than two because the consumer's question is the same either
    way — "what was everything at this instant" — and a second group type would
    force every downstream op to handle both.

    ``missing`` is explicit rather than an absent key: a topic that never published
    and one whose only sample is 10 s stale are different failures, and both differ
    from a healthy value. Staleness (`stale_ns`) is what moves a key into it.
    """

    t_ns: Ns  # the group time — the earliest matched member's stamp
    by_topic: dict[str, Sampled]
    missing: tuple[str, ...] = ()

    def __getitem__(self, topic: str) -> Element:
        """The value itself — the common case. `by_topic` has the provenance."""
        return self.by_topic[topic].element

    def __contains__(self, topic: str) -> bool:
        return topic in self.by_topic

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def residual_ns(self) -> Ns:
        """Worst distance from the group time to any key's real source stamp.

        For matched keys alone this is exactly the old pairwise-spread definition:
        the group time is the earliest member, so the largest ``|t_key - t_ns|``
        IS the spread. Extending it over resampled keys therefore changes no
        existing number while giving one staleness gate over the whole group.
        """
        return max((s.residual_ns for s in self.by_topic.values()), default=0)
