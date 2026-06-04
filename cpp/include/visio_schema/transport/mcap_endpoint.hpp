// McapEndpoint — a sink Endpoint that records messages to MCAP. A transport
// Endpoint (like SerialEndpoint), but its "wire" is an MCAP file: it wraps
// visio_schema::mcap::McapWriter and resolves each message's stream_id to its
// Channel via a resolve(stream_id) -> const Channel* callback (typically a lambda
// over ChannelRegistry::Resolve, with or without a bus). A message whose id does
// not resolve (no DeviceInfo announce seen) is dropped (drop-until-mapped). No
// bus dependency. Mirrors python/visio_schema/transport/mcap_endpoint.py.
#pragma once

#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <string_view>
#include <vector>

#include "visio_schema/mcap/writer.hpp"
#include "visio_schema/routing/channel.hpp"   // Channel
#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/write_policy.hpp"
#include "visio_schema/wire/message.hpp"

namespace visio_schema::transport {

// resolve(stream_id) -> const Channel* (nullptr if unmapped). The pointer must
// stay valid for the Write() call (a ChannelRegistry resolves synchronously).
using StreamResolver = std::function<const Channel*(std::uint32_t)>;

// A recording sink. Write() (bus thread) enqueues into a bounded queue under a
// WritePolicy; the frames are flushed to the MCAP file synchronously on OnTick()
// (also the bus thread — no writer thread) and on Close(). On the embedded board
// a bounded policy (drop-oldest / byte-cap) bounds RAM if the SD card stalls; on
// the host the default lossless policy never drops. Mirrors
// python/visio_schema/transport/mcap_endpoint.py.
class McapEndpoint : public Endpoint {
 public:
  // Record to a filesystem path. `resolve` maps a stream_id to its Channel.
  // max_bytes / max_duration_s rotate into numbered parts (see McapWriter).
  // `policy` bounds the in-RAM queue (default lossless — never drop).
  McapEndpoint(std::string_view path, StreamResolver resolve,
               std::uint64_t max_bytes = 0, double max_duration_s = 0.0,
               WritePolicy policy = WritePolicy::lossless());
  ~McapEndpoint() override;

  McapEndpoint(const McapEndpoint&) = delete;
  McapEndpoint& operator=(const McapEndpoint&) = delete;

  int Fileno() const override { return -1; }      // sink-only, not fd-driven
  std::vector<Message> TryRead() override { return {}; }
  void Write(const Message& msg) override;         // enqueue (bounded)
  void OnTick(std::int64_t now_ns) override;       // flush queue to the file
  void Close() override;                            // flush + finalize

  std::size_t pending_frames() const { return queue_.size(); }
  // Total frames the policy has shed because the storage couldn't keep up. A
  // non-zero, growing value means the recording has gaps (SD card too slow).
  std::uint64_t dropped_frames() const { return dropped_frames_; }

 private:
  void Drain();             // write every queued frame to the MCAP writer (bus thread)
  void NoteDrop(std::size_t n);  // count + throttled-log shed frames

  const StreamResolver resolve_;
  std::unique_ptr<visio_schema::mcap::McapWriter> writer_;
  WritePolicy policy_;
  std::deque<Message> queue_;
  std::size_t queue_bytes_ = 0;
  std::uint64_t dropped_frames_ = 0;
  bool closed_ = false;
};

}  // namespace visio_schema::transport
