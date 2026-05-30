# StreamKind → Payload Type — canonical mapping

Each `visio.wire.v1.StreamKind` value maps **1:1** to a payload
protobuf type. The **canonical source of truth** is the
`(visio_proto_type)` and `(visio_mcap_schema_name)` custom
`EnumValueOptions` annotations on `StreamKind` in
[`proto/visio/wire/v1/header.proto`](../proto/visio/wire/v1/header.proto).
The table below is a human-readable view; if it disagrees with the
proto, fix the proto.

Both implementations (`visio-mq/cpp/` and `visio-mq/python/`) read
these annotations at runtime via the protobuf descriptor API — no
codegen script, no markdown parsing. McapEndpoint reads
`visio_mcap_schema_name` to pick the MCAP `Schema.name` to register
when writing a channel.

Dual-payload service streams (`STREAM_TIMESYNC`, `STREAM_SCHEMA_QUERY`)
carry one of two protobuf types per direction (Request / Response)
and are deliberately left UNannotated — `SchemaRegistry` returns
`None` for them, and the service implementations hand-code the
two-type dispatch.

## Mapping table

| StreamKind value | Wire enum int | Proto full name | MCAP Schema.name | Source |
|---|---:|---|---|---|
| `STREAM_UNKNOWN` | 0 | — (never publish) | — | — |
| **Sensors (1..9)** | | | | |
| `STREAM_IMU_RAW` | 1 | `visio.sensor.v1.ImuRaw` | `visio.sensor.v1.ImuRaw` | visio |
| `STREAM_ENCODER_RAW` | 2 | `visio.sensor.v1.EncoderRaw` | `visio.sensor.v1.EncoderRaw` | visio |
| `STREAM_SYSTEM_HEALTH` | 3 | `visio.sensor.v1.SystemHealth` | `visio.sensor.v1.SystemHealth` | visio |
| `STREAM_BUTTON_EVENT` | 4 | `visio.sensor.v1.ButtonEvent` | `visio.sensor.v1.ButtonEvent` | visio |
| `STREAM_IMU_QUAT` | 5 | `visio.ros.geometry_msgs.v1.Quaternion` | **`geometry_msgs/msg/Quaternion`** | visio (ROS-named) |
| **Vision (10..19)** | | | | |
| `STREAM_VIDEO_COMPRESSED` | 10 | `foxglove.CompressedVideo` | `foxglove.CompressedVideo` | foxglove |
| `STREAM_IMAGE_COMPRESSED` | 11 | `foxglove.CompressedImage` | `foxglove.CompressedImage` | foxglove |
| `STREAM_IMAGE_RAW` | 12 | `foxglove.RawImage` | `foxglove.RawImage` | foxglove |
| `STREAM_CAMERA_CALIB` | 13 | `foxglove.CameraCalibration` | `foxglove.CameraCalibration` | foxglove |
| **Audio (20..29)** | | | | |
| `STREAM_AUDIO_PCM` | 20 | `foxglove.RawAudio` | `foxglove.RawAudio` | foxglove |
| `STREAM_AUDIO_COMPRESSED` | 21 | `visio.sensor.v1.AudioCompressed` | `visio.sensor.v1.AudioCompressed` | visio |
| **Spatial / state (30..39)** | | | | |
| `STREAM_POSE` | 30 | `foxglove.PoseInFrame` | `foxglove.PoseInFrame` | foxglove |
| `STREAM_TWIST` | 31 | `visio.geometry.v1.Twist` | `visio.geometry.v1.Twist` | visio |
| `STREAM_TF` | 32 | `foxglove.FrameTransforms` | `foxglove.FrameTransforms` | foxglove |
| `STREAM_JOINT_STATES` | 33 | `foxglove.JointStates` | `foxglove.JointStates` | foxglove |
| **Input / control (40..49)** | | | | |
| `STREAM_CONTROLLER_STATE` | 40 | `visio.input.v1.QuestControllerState` | `visio.input.v1.QuestControllerState` | visio |
| `STREAM_COMMAND` | 41 | `visio.control.v1.Command` | `visio.control.v1.Command` | visio |
| **Logging (50..59)** | | | | |
| `STREAM_LOG` | 50 | `foxglove.Log` | `foxglove.Log` | foxglove |
| **Services (60..69)** | | | | |
| `STREAM_TIMESYNC` | 60 | `visio.service.timesync.v1.{Request,Response}` (oneof on wire) | `visio.service.timesync.v1.Request` / `Response` | visio |
| `STREAM_DEVICE_INFO` | 61 | `visio.service.device_info.v1.DeviceInfo` | `visio.service.device_info.v1.DeviceInfo` | visio |
| `STREAM_HEARTBEAT` | 62 | `visio.service.heartbeat.v1.Heartbeat` | `visio.service.heartbeat.v1.Heartbeat` | visio |
| `STREAM_SCHEMA_QUERY` | 63 | `visio.service.schema.v1.{Request,Response}` | `visio.service.schema.v1.Request` / `Response` | visio |
| **OSS extension** | | | | |
| `STREAM_CUSTOM` | 100 | (third-party — see DeviceInfo.streams.label) | (custom string per producer) | external |

## Notes on the "MCAP Schema.name" column

For most rows the MCAP `Schema.name` field equals the protobuf full
name. **One exception** is `STREAM_IMU_QUAT`: we register the
Schema record under the **ROS name** `geometry_msgs/msg/Quaternion`
rather than the protobuf full name `visio.ros.geometry_msgs.v1.Quaternion`,
because Foxglove Studio's orientation panels match on the ROS name
string. The protobuf bytes are still parsed via the embedded
`FileDescriptorProto`; only the human-readable name string changes.

This is the pattern any future `visio.ros.*` mimic uses: the proto
package preserves a stable internal identity (`visio.ros.X.v1.Y`),
the MCAP Schema.name preserves the public ROS contract
(`X/msg/Y`).

## Service streams and oneof payloads

`STREAM_TIMESYNC` and `STREAM_SCHEMA_QUERY` carry one of two message
types each (`Request` or `Response`). Producers select by encoding
whichever message they want as the payload; receivers attempt both
parses and route accordingly. The wire `Header.stream` value is the
same for both directions; the distinction is intra-payload.

(An alternative would be a `oneof` wrapper, but it adds 2 bytes
per message for no real benefit since these streams are low-rate.)

## Codegen note

The C++ and Python implementations of `visio-mq` use this table to
emit a static lookup array of the form:

```cpp
struct StreamMapping {
  StreamKind          kind;
  std::string_view    proto_full_name;
  std::string_view    mcap_schema_name;
};
constexpr StreamMapping kStreamMap[] = { /* one row per StreamKind */ };
```

```python
@dataclass(frozen=True)
class StreamMapping:
    kind: StreamKind
    proto_full_name: str
    mcap_schema_name: str

STREAM_MAP: dict[StreamKind, StreamMapping] = { ... }
```

The mappings are generated, not hand-written, from this document. A
script under `visio-mq/tools/gen_stream_map.py` (Phase 1 deliverable)
parses this markdown table and emits both forms.

## How to add a new stream type

1. Add the protobuf message under `proto/visio/*` (or vendor a
   foxglove / ROS-named one).
2. Add the `StreamKind` enum value in
   [`proto/visio/wire/v1/header.proto`](../proto/visio/wire/v1/header.proto)
   in the appropriate category range (1..9 sensors, 10..19 vision,
   etc.). Use the next available number; do NOT renumber existing
   entries (breaking change).
3. Add the row to the table above. Set `proto_full_name`,
   `mcap_schema_name`, and source column.
4. Run `make gen` to verify codegen still passes.
5. Run `make breaking` to confirm no breaking change against `main`.
6. If MCAP-name differs from proto-name (ROS mimic pattern), also
   document the rationale alongside the row.
