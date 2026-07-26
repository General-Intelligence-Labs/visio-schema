# Stream → payload type binding

How a wire stream maps to its payload protobuf type and MCAP/Foxglove schema.

With **dynamic, string-named streams** there is no compile-time `StreamKind`
enum and no generated stream table. A stream is identified globally by its
**topic** and described at runtime by a `Channel` (mirrors the Foxglove channel),
carried in the periodic `DeviceInfo` announce:

```protobuf
message Channel {
  uint32 id = 1;               // per-link stream_id label
  string topic = 2;            // /glove_left/imus/3/raw
  string encoding = 3;         // "protobuf"
  string schema_name = 4;      // visio_schema.v1.sensor.ImuRaw (protobuf full name)
  bytes  schema = 5;           // serialized FileDescriptorSet (payload + deps)
  string schema_encoding = 6;  // "protobuf"
}
```

The binding a consumer needs — `stream_id → (topic, payload type, descriptor)` —
is learned from the announce, not from a table: `schema_name` is the protobuf
full name and `schema` is the `FileDescriptorSet`, so Foxglove/MCAP resolve the
type by looking `schema_name` up inside `schema`. No hand-maintained map, no
fleet reflash to add a stream.

## Topic convention

`/<device>/<sensor-group>/<index>/<sub-field>` — e.g. `/glove_left/imus/3/raw`,
`/glove_left/imus/3/quat`, `/gripper/cam/0/video`. The leading segment is the
device's `equipment_type` (its logical role, e.g. `glove_left`) — not the
per-unit `device_name`, which is a unique addressable label (e.g. `GILABS-<code8>`).

## Adding a stream

A device declares it at runtime — no schema edit:

1. Build the payload's `FileDescriptorSet` from the descriptor pool
   (`visio_schema.wire.schema.file_descriptor_set(proto_type)`), or let
   `visio_schema.make_channel(topic, schema_name, stream_id=...)` build the whole
   `Channel` for you.
2. Declare the output: `router.declare(topic, schema_name, fds)` (or pass it in
   `StreamRouter(bus, channels=[...])`), then publish by topic with
   `bus.publish_stream(topic, payload)` (the router installs the topic resolver).
3. If the payload type is brand-new to the schema, add its generated module to
   `visio_schema.wire.streams._PAYLOAD_MODULES` so the descriptor pool can
   resolve it by name.

## Per-connection stream policy

A consumer does not have to take every stream a device publishes. `SetStreamPolicy`
(`Command` tag 32) installs an ordered filter **scoped to the connection it arrives
on** — recording to disk and every other connected client are untouched, which is
what lets a preview thin its own feed without changing what gets captured.

```
message SetStreamPolicy {
  message Rule { string topic; bool drop; uint32 max_rate_hz; }
  repeated Rule rules = 1;   // <= 12
}
```

Rules are **first match wins**, with an implicit "keep everything else at full rate"
tail. A phone showing one eye of a stereo ego sends:

| topic | effect |
|---|---|
| `**/camera/0` | keep (no drop, no cap) |
| `**/camera/*` | drop — the other eye |
| `**/imu/*/quat` | drop — the ~470 msg/s fused stream nothing renders |

Note what is NOT filtered: the raw IMU bundles. They carry the accel/gyro a
client actually displays and arrive pre-batched at ~29 msg/s, so a rule there
costs samples and saves almost no messages. The per-message framing of the
*derived* quaternion stream is the real device-CPU cost, and dropping it is free
because its ground truth is in the bundles.

Each command **replaces** the connection's policy; rules are never merged with what
came before, so a client sends its full desired state and a lost or reordered command
cannot leave a half-applied filter behind. A fresh connection has no policy and gets
everything — a dropped command costs bandwidth, never data.

`target_device` is ignored. A policy describes what one *link* should carry, so it
applies at the hop it arrives on; on a suit that is the hub→phone leg, which is the
bandwidth that actually binds.

**Topic globs.** The leading `/` is stripped from both sides, then patterns match
segment by segment: `*` is exactly one segment, `**` is zero or more segments at
any position, and anything else is a literal. There are no partial-segment globs
(`cam*` is the literal text `cam*`). Patterns are re-resolved whenever the peer
learns a channel, so a rule also covers leaves a hub discovers later.

Use a **leading `**`** when the topic depth is not yours to know. Relayed leaf
topics normally arrive unchanged — they already carry the leaf's `device_name`
(`/head/camera/0`) — but a relay MAY namespace them under it
(`/GILABS-AABBCCDD/head/camera/0`). That is `prefix_topics_with_device_name`,
**off by default** and opted into for multi-device bring-up, where two same-role
units would collide on every topic. `**/camera/0` matches both forms;
`*/camera/0` only the unprefixed one.

**`max_rate_hz` does not apply to camera video.** H.265 is inter-coded: shedding
arbitrary P-frames costs the decoder its reference chain and blanks the viewer for a
whole GOP. Video is keep-or-drop. Capping is for streams whose messages stand alone —
the fused IMU quaternions (per-sample) and the raw IMU bundles.

`SetVideoStreaming` (23) and `SetImuLiveRate` (31) are the deprecated predecessors.
They keyed off the message class rather than the topic, so neither could name a single
camera and neither could touch the raw IMU bundles. A receiver still honours them by
expanding them into the equivalent policy — drop every `foxglove.CompressedVideo`
channel, cap every `Quaternion` channel — so there is one enforcement path.
