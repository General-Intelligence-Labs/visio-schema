# Foxglove Compatibility — type-usage matrix

Visio borrows schema types from the Foxglove SDK and from ROS where
those ecosystems already define a sensible shape, and defines its
own types only for the gaps. This document explains exactly which
types fall into which bucket, and why.

## Three categories

| Category | Where the schema lives | MCAP `Schema.name` written | Foxglove Studio renders? |
|---|---|---|---|
| **A. Adopt-as-is (Foxglove)** | `third_party/foxglove-sdk/schemas/proto/foxglove/*.proto` | foxglove-native name (e.g. `foxglove.CompressedVideo`) | yes, native |
| **B. ROS-named mimic** | `proto/visio/ros/<package>/v1/*.proto` (we own the proto) | ROS-canonical name (e.g. `geometry_msgs/msg/Quaternion`) | yes, via Studio's ROS-schema panel matching |
| **C. Wholly Visio** | `proto/visio/*` | Visio-native name (e.g. `visio_schema.sensor.v1.ImuRaw`) | not natively; use Plot panel for scalar fields, or write a converter extension |

## Inventory

### Category A — adopt-as-is

| Foxglove type | StreamKind | Why we picked it |
|---|---|---|
| `foxglove.CompressedVideo` | `STREAM_VIDEO_COMPRESSED` | H.264 / H.265 / VP9 / AV1 framed bitstream — exact match for UMI's H.265 gripper output (verified Annex B, IDR + parameter sets inline, no B-frames). |
| `foxglove.CompressedImage` | `STREAM_IMAGE_COMPRESSED` | JPEG / PNG / WebP / AVIF stills. |
| `foxglove.RawImage` | `STREAM_IMAGE_RAW` | Many encodings — NV12, YUV422, RGB/BGR, mono8/16, Bayer. Broad. |
| `foxglove.CameraCalibration` | `STREAM_CAMERA_CALIB` | OpenCV plumb_bob / fisheye / **Project Aria Fisheye62** — Aria support is a real plus for the head-worn rig. |
| `foxglove.RawAudio` | `STREAM_AUDIO_PCM` | PCM-S16 only at the moment; sufficient for the headset mic when not compressing. |
| `foxglove.PoseInFrame` | `STREAM_POSE` | Timestamped pose in a named frame. Direct fit for Quest head / wrist poses. |
| `foxglove.FrameTransforms` | `STREAM_TF` | TF tree updates. Drives Foxglove Studio's 3D panel with URDFs. |
| `foxglove.JointStates` | `STREAM_JOINT_STATES` | Calibrated joint positions — gripper open/close, finger joints. Renders against URDF natively. |
| `foxglove.Log` | `STREAM_LOG` | Structured log records — replaces stderr-tracing for the few diagnostic events we want streamed. |

### Category B — ROS-named mimic

| Visio proto | Wire schema name | MCAP `Schema.name` | StreamKind | Why named-not-canonical |
|---|---|---|---|---|
| `visio_schema.ros.geometry_msgs.v1.Quaternion` | (4 doubles `x, y, z, w` + `timestamp`) | **`geometry_msgs/msg/Quaternion`** | `STREAM_IMU_QUAT` | Foxglove's community orientation panels filter by `schemaName === "geometry_msgs/msg/Quaternion"`. Naming our proto with the ROS string gives us out-of-box panel rendering. The extra `timestamp` field is ignored by the panels (only `x/y/z/w` are read) and used by our own consumers for self-description. |

This pattern (ROS-name on the wire, our own protobuf body) is the
template for any future "Foxglove must recognize this as ROS" type.
See [stream_type_map.md](stream_type_map.md) for the MCAP-name vs
proto-name distinction.

### Category C — wholly Visio

| Visio proto | StreamKind | Why custom |
|---|---|---|
| `visio_schema.sensor.v1.ImuRaw` | `STREAM_IMU_RAW` | Foxglove has NO raw-IMU schema (verified against `foxglove-sdk` v0.24.0). Bundled per-IMU samples preserve per-tick fidelity that UMI's grid-ZOH discarded. |
| `visio_schema.sensor.v1.EncoderRaw` | `STREAM_ENCODER_RAW` | Raw count, magnetic-field magnitude, AGC, magnet-detection enum — diagnostics for bring-up. Foxglove has nothing like it; `foxglove.JointState` only covers cooked position/velocity/effort. |
| `visio_schema.sensor.v1.SystemHealth` | `STREAM_SYSTEM_HEALTH` | CPU / RAM / battery / disk / stream-clients periodic telemetry. No Foxglove equivalent. |
| `visio_schema.sensor.v1.AudioCompressed` | `STREAM_AUDIO_COMPRESSED` | `foxglove.RawAudio` only supports PCM-S16. We need AAC-LC ADTS / Opus / Vorbis for the UMI headset mic path. |
| `visio_schema.sensor.v1.ButtonEvent` | `STREAM_BUTTON_EVENT` | Named-button edge events; no Foxglove equivalent. |
| `visio_schema.input.v1.QuestControllerState` | `STREAM_CONTROLLER_STATE` | Quest controller buttons + sticks for BOTH hands per tick. No Foxglove equivalent (`foxglove.JointStates` is the closest but joints are continuous, not buttons). |
| `visio_schema.control.v1.Command` | `STREAM_COMMAND` | Host→device intents (StartRecording, StopRecording, Identify). Application-specific. |
| `visio_schema.geometry.v1.Twist` | `STREAM_TWIST` | 6-DoF velocity. Foxglove only has linear `Velocity3`; no Twist. We use `foxglove.Vector3` primitives so it composes. |
| `visio_schema.service.timesync.v1.{Request,Response}` | `STREAM_TIMESYNC` | NTP-style exchange; bus-layer concern, no analogue. |
| `visio_schema.service.device_info.v1.{Request,Response}` | `STREAM_DEVICE_INFO` | Discovery + identity + stream-capability declaration + inline `file_descriptor_sets` (descriptors ride with the Response — no separate schema-query service). |
| `visio_schema.service.heartbeat.v1.Heartbeat` | `STREAM_HEARTBEAT` | Liveness + backpressure hint. |
| `visio_schema.calibration.v1.ImuCalibration` | `STREAM_IMU_CALIBRATION` | Per-IMU bias / scale / mounting pose. Published as a regular stream message (Foxglove convention — same shape as `foxglove.CameraCalibration` on `STREAM_CAMERA_CALIB`). |

## Foxglove submodule policy

The `third_party/foxglove-sdk` submodule is pinned to a specific
release tag (currently `sdk/v0.24.0`, commit `429c2810`). We consume
**only** `third_party/foxglove-sdk/schemas/proto/foxglove/*.proto` —
everything else in the SDK (the various language SDK runtimes, the
docker setups, etc.) is ignored.

### Bump procedure

1. Open a `visio-schema` PR with the submodule update only — no other
   schema changes.
2. Run `make breaking` against `main` to confirm no foxglove-side
   field renames / removals would break us. (buf's breaking check
   doesn't traverse the submodule directly, but we run a local
   diff of the schema files we depend on as part of CI.)
3. Bump `visio-schema` minor version. Any peer using
   `visio_schema.wire.v1.Header.stream` enum values that map to foxglove
   types now sees the new field set; old peers continue to work
   because protobuf forward-compatibility tolerates unknown fields.
4. Downstream `visio` regenerates bindings on the next pull. No
   API change unless we explicitly start using newly-introduced
   foxglove fields.

The submodule bump is **always** a deliberate PR. We never auto-update.

## What this matrix is NOT

- It is NOT a list of every Foxglove type. The full list of
  `foxglove.*` schemas is in
  `third_party/foxglove-sdk/schemas/proto/foxglove/` (46 .proto
  files); we adopt only what we actively need.
- It is NOT a list of every ROS type we mimic. As of v0.1.0 the only
  mimic is `geometry_msgs/msg/Quaternion`. Future additions follow
  the same pattern; document them here when they land.
- It does NOT cover how to write a Foxglove converter extension. That
  belongs in `visio`'s docs if/when we ship one.
