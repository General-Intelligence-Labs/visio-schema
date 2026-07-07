// QueueEndpoint — in-process consumer sink (active object, but synchronous: no
// I/O thread). Backed by a MessageInbox so a non-bus consumer can either drain
// everything queued right now (PopAll — the historical behavior) or block for a
// bounded batch (PopBatch, mutex/GIL released) — the pull-sink read path.
//
// The default policy is lossless (unbounded): the historical behavior the test
// suite relies on (attach → route → PopAll → assert, tiny queues). A live pull
// sink passes a bounded WritePolicy (drop_oldest) so a slow consumer sheds
// oldest instead of growing RAM without bound (video).
#pragma once

#include <cstdint>
#include <optional>
#include <vector>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/message_inbox.hpp"
#include "visio_schema/transport/write_policy.hpp"

namespace visio_schema::transport {

class QueueEndpoint : public Endpoint {
 public:
  explicit QueueEndpoint(std::optional<std::uint32_t> stream_filter = std::nullopt,
                         WritePolicy policy = WritePolicy::lossless())
      : filter_(stream_filter), inbox_(policy) {}

  void Start(InboundFn /*on_inbound*/, ClosedFn /*on_closed*/) override {}
  void Stop() override {}

  void Send(const Message& msg) override {
    if (filter_ && msg.stream_id != *filter_) return;
    inbox_.Push(Message(msg));  // Send owns a const ref; copy, then move into the inbox
  }

  // Non-blocking: drain everything queued right now (the historical PopAll).
  std::vector<Message> PopAll() { return inbox_.PopBatch(/*timeout_ms=*/0, /*max=*/0); }

  // Blocking: wait up to timeout_ms for at least one message, then take the batch
  // (mutex/GIL released across the wait — the pull-sink read path).
  std::vector<Message> PopBatch(int timeout_ms) {
    return inbox_.PopBatch(timeout_ms, /*max=*/0);
  }

  std::size_t Size() { return inbox_.size(); }
  std::uint64_t Dropped() const { return inbox_.Dropped(); }

 private:
  std::optional<std::uint32_t> filter_;
  MessageInbox inbox_;
};

}  // namespace visio_schema::transport
