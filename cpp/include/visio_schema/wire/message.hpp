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
#include <memory>
#include <string>
#include <vector>

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

  // In-memory only (NOT serialized): a live sink MAY rate-limit this message
  // per its endpoint's live-rate setting (SetImuLiveRate). Set by the producer
  // for per-sample derived streams (fused IMU quaternions) whose ground truth
  // ships full-rate elsewhere (the raw bundles). Recording sinks ignore it —
  // recordings stay lossless.
  bool decimatable = false;

  // In-memory only (NOT serialized): cache of EncodeFramed(*this), filled by
  // the FIRST framed sink to send this message and reused by every other one.
  // Outbound framed bytes are byte-identical across sinks (the header is
  // stamped before fanout; per-link stream-id remap happens on hub INBOUND,
  // never per-sink), so one COBS+CRC pass serves the whole fanout. Safe
  // without locking: Bus::Relay hands the same Message to sinks sequentially
  // under its dispatch lock. `mutable` so Send(const Message&) can fill it.
  mutable std::shared_ptr<const std::vector<std::uint8_t>> framed;
};

}  // namespace visio_schema::wire
