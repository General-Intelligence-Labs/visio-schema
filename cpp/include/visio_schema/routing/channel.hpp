// Channel + the routing value types. Mirrors python/visio_schema/routing/channel.py.
//
// Channel mirrors visio_schema.service.device_info.v1.Channel field-for-field
// (itself a mirror of the Foxglove channel); the nanopb type is used only at the
// DeviceInfo encode/decode boundary, so everything else works with this plain
// struct. Routed is the per-message decision the registry returns;
// DuplicateTopicError is the unique-topic invariant.
#pragma once

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>

#include "visio_schema/wire/message.hpp"

namespace visio_schema {

// Default payload/schema encoding for a Channel (the Foxglove + Python default).
// Centralized so the fallback isn't a scattered string literal.
inline constexpr const char* kDefaultEncoding = "protobuf";

struct Channel {
  std::uint32_t id = 0;
  std::string topic;
  std::string encoding = kDefaultEncoding;
  std::string schema_name;
  std::string schema;            // serialized google.protobuf.FileDescriptorSet
  std::string schema_encoding = kDefaultEncoding;
};

namespace routing {

using visio_schema::wire::Message;

// A topic was announced under a second stream id while the first is still live —
// a real protocol/wiring fault (the bus deregisters a dropped link before any
// reconnect re-announce).
class DuplicateTopicError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

// Outcome of Accept: `message` is what to forward (nullopt = drop/absorb);
// `channel` is what to record against (nullptr = skip).
struct Routed {
  std::optional<Message> message;
  const Channel* channel = nullptr;
};

}  // namespace routing
}  // namespace visio_schema
