// Neutral in-memory Visio message — the visio_schema.wire.v1.Header fields +
// payload, with helpers to map to/from the Header protobuf. Carries no
// bus/transport semantics; higher layers (e.g. visio-mq's Bus) own
// sequence stamping and the timesync `timestamp` rewrite.
#pragma once

#include <cstdint>
#include <string>
#include <utility>

#include "google/protobuf/timestamp.pb.h"
#include "visio_schema/wire/v1/header.pb.h"

namespace visio_schema::wire {

struct Message {
  visio_schema::wire::v1::DeviceClass device       = visio_schema::wire::v1::DEVICE_UNKNOWN;
  visio_schema::wire::v1::DeviceClass routed_from  = visio_schema::wire::v1::DEVICE_UNKNOWN;
  visio_schema::wire::v1::StreamKind  stream       = visio_schema::wire::v1::STREAM_UNKNOWN;
  std::uint32_t                stream_index = 0;   // semantically uint8 [0, 255]
  std::uint32_t                seq          = 0;
  google::protobuf::Timestamp  timestamp{};

  // Inner protobuf bytes for the StreamKind's mapped type.
  std::string payload;
};

inline visio_schema::wire::v1::Header MsgToHeader(const Message& msg) {
  visio_schema::wire::v1::Header h;
  h.set_device(msg.device);
  h.set_routed_from(msg.routed_from);
  h.set_stream(msg.stream);
  h.set_stream_index(msg.stream_index);
  h.set_seq(msg.seq);
  *h.mutable_timestamp() = msg.timestamp;
  return h;
}

inline Message HeaderToMsg(const visio_schema::wire::v1::Header& h, std::string payload) {
  Message m;
  m.device = h.device();
  m.routed_from = h.routed_from();
  m.stream = h.stream();
  m.stream_index = h.stream_index();
  m.seq = h.seq();
  m.timestamp = h.timestamp();
  m.payload = std::move(payload);
  return m;
}

}  // namespace visio_schema::wire
