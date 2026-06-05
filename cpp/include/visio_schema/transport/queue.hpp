// QueueEndpoint — in-process consumer sink (active object, but synchronous: no
// thread). Send() appends under a lock so a non-bus thread can drain via PopAll.
#pragma once

#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <vector>

#include "visio_schema/transport/endpoint.hpp"

namespace visio_schema::transport {

class QueueEndpoint : public Endpoint {
 public:
  explicit QueueEndpoint(std::optional<std::uint32_t> stream_filter = std::nullopt)
      : filter_(stream_filter) {}

  void Start(InboundFn /*on_inbound*/, ClosedFn /*on_closed*/) override {}
  void Stop() override {}

  void Send(const Message& msg) override {
    if (filter_ && msg.stream_id != *filter_) return;
    std::lock_guard<std::mutex> lock(mu_);
    queue_.push_back(msg);
  }

  std::vector<Message> PopAll() {
    std::lock_guard<std::mutex> lock(mu_);
    std::vector<Message> out(queue_.begin(), queue_.end());
    queue_.clear();
    return out;
  }

  std::size_t Size() {
    std::lock_guard<std::mutex> lock(mu_);
    return queue_.size();
  }

 private:
  std::optional<std::uint32_t> filter_;
  std::deque<Message> queue_;
  std::mutex mu_;
};

}  // namespace visio_schema::transport
