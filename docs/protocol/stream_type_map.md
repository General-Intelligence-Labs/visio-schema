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
device's `device_name`.

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
