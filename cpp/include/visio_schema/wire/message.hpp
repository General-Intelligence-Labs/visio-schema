// In-memory Visio wire message: the visio_schema.v1.wire.Header fields plus
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
#include "visio_schema/v1/wire/header.pb.h"   // nanopb: Header + ControlStream

namespace visio_schema::wire {

// A stream is named globally by a topic string and labelled on the wire by a
// compact per-link `stream_id` (control ids < CONTROL_STREAM_FIRST_DYNAMIC are
// hop-local; data ids are negotiated and hub-remapped).
struct Message {
  std::uint32_t stream_id = 0;
  std::uint32_t seq = 0;
  google_protobuf_Timestamp timestamp = google_protobuf_Timestamp_init_zero;

  std::string payload;

  // In-memory only (NOT serialized into the wire Header): marks a high-bandwidth
  // bulk stream (camera video). A split-outbox endpoint sends non-bulk CONTROL
  // frames (command results, DeviceInfo, OTA status, IMU) on a separate queue
  // ahead of video, so a reply isn't stuck behind seconds of buffered H.265 on a
  // bandwidth-limited link. Set by the producer (publish_video).
  bool bulk = false;

  // In-memory only (NOT serialized): this bulk frame is a SYNC POINT — an H.265
  // keyframe carrying VPS/SPS/PPS. A bounded outbox must never evict one: losing
  // a P-frame costs a frame, losing a keyframe costs the decoder its reference
  // chain and blanks the viewer until the next one (a whole GOP). Set by the
  // producer alongside `bulk`.
  bool keyframe = false;
};

}  // namespace visio_schema::wire
