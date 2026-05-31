// In-memory Visio wire message: the visio_schema.wire.v1.Header fields plus
// the opaque payload bytes.
//
// The C++ wire library is nanopb-only (no full libprotobuf) so it links on the
// RV1106. Fields use the nanopb-generated enum + Timestamp types directly;
// `payload` is the inner message's serialized bytes, which this layer never
// parses. Higher layers (e.g. the Bus) own sequence stamping and the timesync
// `timestamp` rewrite.
#pragma once

#include <cstdint>
#include <string>

#include "google/protobuf/timestamp.pb.h"    // nanopb: google_protobuf_Timestamp
#include "visio_schema/wire/v1/header.pb.h"   // nanopb: DeviceClass/StreamKind/Header

namespace visio_schema::wire {

struct Message {
  visio_schema_wire_v1_DeviceClass device =
      visio_schema_wire_v1_DeviceClass_DEVICE_UNKNOWN;
  visio_schema_wire_v1_DeviceClass routed_from =
      visio_schema_wire_v1_DeviceClass_DEVICE_UNKNOWN;
  visio_schema_wire_v1_StreamKind stream =
      visio_schema_wire_v1_StreamKind_STREAM_UNKNOWN;
  std::uint32_t stream_index = 0;   // semantically uint8 [0, 255]
  std::uint32_t seq = 0;
  google_protobuf_Timestamp timestamp = google_protobuf_Timestamp_init_zero;

  std::string payload;
};

}  // namespace visio_schema::wire
