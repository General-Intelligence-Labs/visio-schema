// McapEndpoint — a sink Endpoint that records messages to MCAP. A transport
// Endpoint (like SerialEndpoint), but its "wire" is an MCAP file: it wraps
// visio_schema::mcap::McapWriter and resolves each message's stream_id to its
// Channel via a resolve(stream_id) -> const Channel* callback (typically a lambda
// over ChannelRegistry::Resolve, with or without a bus). A message whose id does
// not resolve (no DeviceInfo announce seen) is dropped (drop-until-mapped). No
// bus dependency. Mirrors python/visio_schema/transport/mcap_endpoint.py.
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string_view>
#include <vector>

#include "visio_schema/mcap/writer.hpp"
#include "visio_schema/routing/channel.hpp"   // Channel
#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/wire/message.hpp"

namespace visio_schema::transport {

// resolve(stream_id) -> const Channel* (nullptr if unmapped). The pointer must
// stay valid for the Write() call (a ChannelRegistry resolves synchronously).
using StreamResolver = std::function<const Channel*(std::uint32_t)>;

class McapEndpoint : public Endpoint {
 public:
  // Record to a filesystem path. `resolve` maps a stream_id to its Channel.
  // max_bytes / max_duration_s rotate into numbered parts (see McapWriter).
  McapEndpoint(std::string_view path, StreamResolver resolve,
               std::uint64_t max_bytes = 0, double max_duration_s = 0.0);
  ~McapEndpoint() override;

  McapEndpoint(const McapEndpoint&) = delete;
  McapEndpoint& operator=(const McapEndpoint&) = delete;

  int Fileno() const override { return -1; }      // sink-only, not fd-driven
  std::vector<Message> TryRead() override { return {}; }
  void Write(const Message& msg) override;
  void Close() override;

 private:
  const StreamResolver resolve_;
  std::unique_ptr<visio_schema::mcap::McapWriter> writer_;
  bool closed_ = false;
};

}  // namespace visio_schema::transport
