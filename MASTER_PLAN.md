# visio-schema — master plan

The public contract for the Visio sensor/data ecosystem. Defines every
message type, the wire envelope, the per-transport wire framing rules,
and the timesync algorithm. Both `visio-mq` (C++ and Python) and any
third-party consumer depend on this repo.

This document is the canonical statement of intent for the repo. If
something here contradicts code, the document wins until updated.

---

## 1. Purpose

- Single source of truth for protobuf message definitions.
- Single source of truth for per-transport wire framing.
- Single source of truth for the timesync algorithm.
- Codegen for multiple languages (C++, Python, Java, Swift).
- Stable versioned releases that downstream consumers can pin.

This repo contains ONLY schema, codegen plumbing, and specs. No
transport implementation, no bus, no MCAP code — those live in
`visio-mq`.

## 2. Scope boundaries (what this repo is NOT)

- Not a transport library.
- Not a recording library.
- Not an SDK with batteries — just the contract + spec docs.
- Not version-tied to any specific consumer; consumers pin a version.
- No application logic, no service implementations, no examples beyond
  what is needed to validate codegen.

## 3. Repo layout

```
visio-schema/
├── MASTER_PLAN.md         this file
├── buf.yaml               buf module config
├── buf.gen.yaml           codegen plugins (cpp, py, java, swift)
├── Makefile               `make gen` / `make lint` / `make breaking`
├── .gitmodules
├── docs/
│   ├── framing.md           per-transport wire byte specs +
│                            Header (protobuf) + HEADER_LEN convention
│                            (CANONICAL)
│   ├── timesync.md          NTP-style algorithm spec (CANONICAL)
│   ├── versioning.md        semver + buf breaking-change policy
│   ├── stream_type_map.md   StreamKind -> protobuf full name mapping
│                            (CANONICAL; both impls generate static
│                            tables from this)
│   └── foxglove_compat.md   which Foxglove types we adopt as-is, which
│                            we pattern after, which are entirely ours
├── third_party/
│   └── foxglove-sdk/      git submodule, pinned to sdk/v0.24.0
│                          (we consume schemas/proto/foxglove/*.proto;
│                          everything else in the submodule is ignored)
├── proto/
│   └── visio/             our own namespace, gap-fillers only
│       ├── wire/v1/header.proto           Header message + DeviceClass
│       │                                  and StreamKind enums.
│       │                                  Per-Endpoint frame wrappers
│       │                                  spec in docs/framing.md.
│       ├── service/
│       │   ├── timesync/v1/timesync.proto       T_CLOCK_SYNC parity
│       │   ├── device_info/v1/device_info.proto T_DEVICE_INFO_* parity
│       │   ├── heartbeat/v1/heartbeat.proto     T_HEART parity
│       │   └── schema/v1/schema.proto           descriptor query
│       ├── sensor/v1/
│       │   ├── imu_raw.proto              T_IMU raw fields (bundled,
│       │   │                              no frame_id — body frame
│       │   │                              implicit per stream_index)
│       │   │                              (fused quat path uses
│       │   │                              visio.ros.geometry_msgs.v1.
│       │   │                              Quaternion for Foxglove
│       │   │                              orientation panel rendering)
│       │   ├── encoder_raw.proto          T_POSITION_ENCODER raw
│       │   │                              diagnostics (bundled). Cooked
│       │   │                              joint position publishes on
│       │   │                              STREAM_JOINT_STATES as
│       │   │                              foxglove.JointStates for
│       │   │                              Foxglove URDF rendering.
│       │   ├── system_health.proto        T_SYSTEM parity
│       │   ├── audio_compressed.proto     T_AUDIO parity (AAC/Opus)
│       │   └── button.proto               T_CONTROL parity (a button is
│       │                                  a sensor; consumer interprets)
│       ├── control/v1/
│       │   └── command.proto              host->device commands
│       ├── input/v1/
│       │   └── quest_controller_state.proto  T_POSE button/stick fields
│       ├── geometry/v1/
│       │   └── twist.proto                T_POSE velocity fields
│       └── ros/                           Protobuf mimics of canonical
│           └── geometry_msgs/v1/          ROS message types, registered
│               └── quaternion.proto       in MCAP under their ROS schema
│                                          names so Foxglove panels render
│                                          natively (used by STREAM_IMU_QUAT)
└── gen/                   generated per-language outputs
                           (gitignored at HEAD; vendored at release tags
                           for downstream zero-codegen consumption)
```

`buf.yaml` declares two roots for proto resolution:
- `third_party/foxglove-sdk/schemas/proto`  (for `foxglove.*` imports)
- `proto`                                    (for `visio.*` imports)

## 4. Core decisions (locked from design conversation)

- proto3, our own namespace is `visio.*`.
- We reuse `foxglove.*` schemas wherever Foxglove already defines a
  reasonable type. See section 4a below.
- Per-domain versioning via package path (`sensor/v1`, `sensor/v2`);
  introduce v2 alongside v1 for graceful migration; never mutate v1
  semantics.
- buf for lint, generate, and breaking-change detection.
- **Wire identity is enum-encoded, not string-encoded.** Every peer
  is a `DeviceClass` value; every payload type is a `StreamKind` value;
  topics are derived presentation strings synthesized only at the MCAP
  / Foxglove layer. Left/right and similar instance distinctions are
  separate enum values (no `device_index` field on the wire).
- **The wire Header is a small protobuf message**
  (`visio.wire.v1.Header`) preceded by a 2-byte little-endian
  `HEADER_LEN` field on every framed Endpoint. Protobuf gives us
  evolvability (optional fields without a wire-format break) at the
  cost of ~3-8 extra bytes per message vs fixed-binary; we accept
  that tradeoff for the header's small surface.
- Header carries one timestamp (`timestamp`, a
  `google.protobuf.Timestamp`, rewritten on rx by the receiver's
  Timesync service to land in the receiver's local clock domain). The
  producer's original timestamp lives **inside the payload** for
  self-contained replay and is never duplicated in the header. All
  Visio schemas use `google.protobuf.Timestamp` consistently —
  exception: `visio.service.timesync.v1` uses bare `uint64` for
  `t0`/`t1`/`t2` because the NTP math operates on raw mono integers.
- Relays are supported: each hop's receiver applies the Timesync
  offset for `routed_from` (not `device`) to rewrite `timestamp`.
- Spatial messages use a `frame_id` string and a TF tree published as
  `foxglove.FrameTransforms` messages on the reserved `STREAM_TF`
  stream (ROS / Foxglove convention; makes Foxglove Studio's 3D panel
  work out of the box).
- **Raw IMU and fused orientation are separate, bundled streams, never
  resampled at the publisher.**
  - `STREAM_IMU_RAW` (`visio.sensor.v1.ImuRaw`) carries bundled raw
    gyro/accel/(mag)/temp samples from one IMU instance. Each sample
    carries its own `t_offset_ns` captured at sensor read time. One
    bundle is emitted per FIFO drain (~60 Hz on UMI hardware,
    ~3-4 samples per bundle at 200 Hz gyro ODR).
  - `STREAM_IMU_QUAT` carries per-sample fused orientation as
    `visio.ros.geometry_msgs.v1.Quaternion` — a strict 4-double
    (x, y, z, w) mimic of ROS's `geometry_msgs/msg/Quaternion`.
    McapEndpoint registers the MCAP Schema record with
    `name = "geometry_msgs/msg/Quaternion"`, so Foxglove's
    orientation panels recognize and render natively. No bundling
    on this stream (panels match only when x/y/z/w sit at the
    message root). No `frame_id` inside the payload — each IMU's
    body frame is implicit per (DeviceClass, stream_index) and the
    world frame is always gravity-aligned. Carries an inner
    `google.protobuf.Timestamp` for self-description, consistent with
    every other Visio payload schema (Foxglove panels ignore it; our
    consumers use it).
  - Consumers correlate raw and quat via per-payload `timestamp`
    fields. This is a deliberate departure from UMI v3, where the
    grid-tick emitter zero-order-held both raw and fused to a single
    rate, silently discarding any intra-grid raw samples buffered in
    the IMU's OutputRing.
- **Same split pattern for encoders.** `STREAM_ENCODER_RAW`
  (`visio.sensor.v1.EncoderRaw`) carries bundled raw sensor readouts —
  raw count, magnetic-field magnitude, AGC, magnet-detection status —
  for bring-up and field debugging. The calibrated joint position
  (the [0, 1] normalized open/close value that gripper-control and
  training pipelines actually consume) publishes on
  `STREAM_JOINT_STATES` as `foxglove.JointStates`, so Foxglove Studio
  renders a URDF-driven gripper animation natively when a URDF is
  loaded.

## 4a. Foxglove schemas as upstream dependency

`third_party/foxglove-sdk` is a git submodule pinned to a specific
release tag (currently `sdk/v0.24.0`). We consume only
`schemas/proto/foxglove/*.proto`; the rest of the SDK is ignored. Our
`buf.yaml` adds the schemas root as a second proto resolution root, so
`visio.*` messages can `import "foxglove/Vector3.proto"` etc. directly.

Three categories of message reuse:

| Category | Examples | Visio's role |
|---|---|---|
| **Adopt as-is** | `foxglove.CompressedVideo`, `CompressedImage`, `RawImage`, `CameraCalibration`, `PoseInFrame`, `FrameTransform[s]`, `JointState[s]`, `Vector3`, `Quaternion`, `Pose`, `Log` | Publish under the Foxglove type name on Visio topics. Wire envelope's `payload_type` is e.g. `"foxglove.CompressedVideo"`. McapEndpoint registers the foxglove schema record verbatim. Foxglove Studio reads our MCAPs natively. |
| **Pattern after** | `visio.sensor.v1.Imu` (no Foxglove equivalent; mirror `sensor_msgs/Imu` shape but use `foxglove.Quaternion` / `foxglove.Vector3` primitives so it composes) | Closest analogue is ROS's `sensor_msgs/Imu`; we follow it for field semantics but stay in our namespace + reuse Foxglove primitives where possible. |
| **Wholly ours** | `visio.sensor.v1.Encoder`, `Tactile`; `visio.control.v1.*`; `visio.service.*`; `visio.wire.v1.Envelope`; (optional) `visio.sensor.v1.AudioCompressed` | Application-specific or transport-internal; Foxglove has no opinion. |

Two timestamps tolerated by design: Foxglove messages carry an inner
`google.protobuf.Timestamp` (sensor acquisition time). That overlaps
semantically with our envelope's `producer_ts_ns`. Both must be set,
both mean the same thing. Cheap redundancy that lets MCAP payloads
remain self-contained for non-Visio readers.

Submodule bump policy: only bump on a deliberate `visio-schema` PR with
spec review. Downstream `visio-mq` regenerates bindings against the
new submodule commit on the next pull. See `docs/foxglove_compat.md`
for the full type-mapping table and bump procedure.

## 5. Wire Header (v1, protobuf)

`visio.wire.v1.Header` is a small protobuf message defined in
`proto/visio/wire/v1/header.proto`. Shape:

```
message Header {
  DeviceClass               device       = 1;   // origin (immutable)
  DeviceClass               routed_from  = 2;   // last hop (rewritten by relays)
  StreamKind                stream       = 3;   // determines payload type 1:1
  uint32                    stream_index = 4;   // uint8 range [0, 255]
  uint32                    seq          = 5;   // per-(device, stream, stream_index)
  google.protobuf.Timestamp timestamp    = 6;   // rewritten on every rx
}
```

All enum values are constrained to [0, 127] so each enum field
encodes to exactly 2 bytes (1 tag + 1 varint value). `stream_index`
is semantically uint8 (the impl validates `<= 255`); values 0..127
encode as 1-byte varint (the practical hot path).

### Byte budget

| Field              | Bytes (typical) |
|--------------------|-----------------|
| `device`           | 2               |
| `routed_from`      | 2               |
| `stream`           | 2               |
| `stream_index`     | 2 (1-byte varint when < 128) |
| `seq`              | 2-6 (varint scales with value) |
| `timestamp`        | 11-13 (Timestamp sub-message: outer tag + len prefix + seconds-varint + nanos-varint) |
| **Header total**   | **~21-25 bytes** |

`google.protobuf.Timestamp` costs ~3 more bytes than a bare `uint64`
ns would, due to nested-message framing (length prefix + two inner
field tags). That cost is paid once per message for whole-schema
consistency with Foxglove and with our own payload schemas.

### Wire frame format (per Endpoint)

The Header is preceded by a 2-byte little-endian `HEADER_LEN` field
giving the size of the serialized Header. The payload bytes follow
immediately. CRC16 covers `HEADER_LEN || header || payload`.

```
TCP:
[ TOTAL_LEN:u32_le ][ HEADER_LEN:u16_le ][ header_pb:N ]
                   [ payload:M ][ CRC16:u16_le ]

Serial (COBS):
[ COBS( [ HEADER_LEN:u16_le ][ header_pb:N ]
        [ payload:M ][ CRC16:u16_le ] ) ][ 0x00 ]

UDP:
[ HEADER_LEN:u16_le ][ header_pb:N ][ payload:M ][ CRC16:u16_le ]
   per datagram

MCAP, WebSocket: native record/frame boundaries; no Visio framing.
```

`HEADER_LEN` is `u16` (not `u8`) so the Header can grow with optional
fields without a wire-format break — that evolvability is the whole
reason we kept it protobuf.

### McapEndpoint field mapping

McapEndpoint does NOT serialize the Header on disk; it maps each
field onto MCAP's own metadata:

| Header field      | MCAP destination                                  |
|-------------------|---------------------------------------------------|
| `device`          | encoded in synthesized topic string + channel metadata |
| `stream`          | resolved to protobuf type name via StreamKind table; written as MCAP Schema record on first sight |
| `stream_index`    | encoded in synthesized topic string                |
| `seq`             | MCAP `Message.sequence`                            |
| `timestamp`       | MCAP `log_time` (Unix-converted at write time) AND `publish_time` (same value; the precise sensor-acquisition time is preserved inside the payload bytes) |
| `routed_from`   | dropped on disk (transport-only metadata)          |
| payload bytes     | MCAP `Message.data` verbatim (no re-wrap)          |

Synthesized topic string format:
```
/{DeviceClass.name_lower}/{StreamKind.name_lower}/{stream_index}
e.g. "/gripper_left/imu/0", "/glove_left/imu/3",
     "/quest/pose/0", "/gripper_right/video_compressed/0"
```

The StreamKind -> proto full name mapping is the canonical contract;
it lives in `docs/stream_type_map.md` and is mirrored as a generated
static array in each language binding.

## 6. Codegen toolchain

- buf for everything: `buf lint`, `buf generate`, `buf breaking`.
- Plugins:
  - C++: `protoc-gen-cpp` (header-only outputs vendored into
    `visio-mq/cpp/third_party/` at consumer side).
  - Python: `protoc-gen-python` + `protoc-gen-mypy` for type stubs.
  - Java: `protoc-gen-java` (post-Phase 2).
  - Swift: `protoc-gen-swift` (post-Phase 2).
- Output language packages:
  - C++: header-only + CMake target.
  - Python: wheel published as `visio-schema` (abi3).
  - Java: Maven (TBD).
  - Swift: SwiftPM (TBD).

## 7. Versioning

- Repo tags follow semver: `v0.1.0`, `v0.2.0`, ..., `v1.0.0`.
- Message-level versioning via proto package (`visio.sensor.v1.*`,
  `visio.sensor.v2.*`).
- `buf breaking` gates any PR touching `proto/`.
- Spec docs (`docs/framing.md`, `docs/timesync.md`) are part of the
  contract; changes to them follow the same semver rules even when no
  `.proto` files change.
- Detailed policy in `docs/versioning.md`.

## 8. Phasing

### Phase 0 — bootstrap (current state)

- MASTER_PLAN.md exists.
- `third_party/foxglove-sdk` submodule pinned to `sdk/v0.24.0`.
- All Phase 1 `.proto` files exist (full UMI v3 parity, minus tactile):
  - `wire/v1/header.proto`
  - `service/{timesync,device_info,heartbeat,schema}/v1/*.proto`
  - `sensor/v1/{imu,encoder,system_health,audio_compressed}.proto`
  - `control/v1/{button,command}.proto`
  - `input/v1/quest_controller_state.proto`
  - `geometry/v1/twist.proto`
- No buf config yet.

### Phase 1 — codegen and docs

- `buf.yaml` (two roots: `third_party/foxglove-sdk/schemas/proto` and
  `proto`), `buf.gen.yaml`, `Makefile`.
- `docs/framing.md`, the canonical wire spec:
  - Protobuf Header preceded by 2-byte `HEADER_LEN` (section 5 of
    this doc).
  - Per-Endpoint frame wrappers:
    - Serial: `[ COBS( [HEADER_LEN:u16][header_pb][payload][CRC16] ) ][ 0x00 ]`
      — COBS delimits the outer frame.
    - TCP: `[ TOTAL_LEN:u32_le ][ HEADER_LEN:u16_le ][ header_pb ][ payload ][ CRC16 ]`.
    - UDP: `[ HEADER_LEN:u16_le ][ header_pb ][ payload ][ CRC16 ]` per datagram.
    - MCAP, WebSocket: native record/frame boundaries; no Visio framing.
  - CRC algorithm: CRC-16/CCITT-FALSE (poly `0x1021`, init `0xFFFF`,
    no reflection, no XOR-out). Computed over
    `HEADER_LEN || header_pb || payload`.
- `docs/timesync.md`: NTP-style t0/t1/t2/t3 + sliding-window offset
  filter + offset-table semantics (keyed by `routed_from`).
- `docs/stream_type_map.md`: canonical `StreamKind` -> protobuf full
  name table. Read by both impls at codegen time to emit static arrays.
- `docs/foxglove_compat.md`: type-mapping table + submodule bump
  procedure.
- `docs/versioning.md`: semver policy + `buf breaking` enforcement.
- C++ + Python codegen targets working end-to-end against both
  `visio.*` and `foxglove.*` types.
- First release tag: `v0.1.0`.

### Phase 2 — language coverage + tooling

- Java, Swift codegen targets.
- Wheel / Maven / SwiftPM release automation.
- Tag `v1.0.0` (first stable contract).

### Phase 3 — post-launch additions (reserved)

- Sensor-kind additions in the 4..9 / 14..19 / 22..29 reserved ranges.
- Stream type variants for new vision codecs (e.g., HEIC).
- Bus extensions discovered during the v1 deployment.

## 9. Conformance

- The `.proto` files **and** the `docs/` specs together form the
  conformance contract.
- `visio-mq` (C++ and Python) MUST conform.
- Cross-language interop tests live in `visio-mq/tests/interop/` and
  validate both impls against the same contract.
- Any change to `docs/framing.md` or `docs/timesync.md` is a breaking
  change at the spec level, even if `.proto` files are untouched.

## 10. Explicit non-goals

- Transport code (TCP, Serial, USB CDC, MCAP) — lives in `visio-mq`.
- Bus, Endpoint, Service classes — live in `visio-mq`.
- CLI tools — live in `visio-mq`.
- Recording / replay logic — lives in `visio-mq`.
- Migration tooling from UMI v3 — lives in
  `visio-mq/tools/v3_bridge/` if and when needed.
- ROS / DDS bridges.
- A schema registry server. (The on-bus `SchemaQueryService` covers
  ad-hoc descriptor fetch; nothing more.)
