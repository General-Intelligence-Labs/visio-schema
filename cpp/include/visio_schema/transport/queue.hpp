// QueueEndpoint — in-process consumer sink. Attach as a sink to capture
// messages for local consumption; thread-safe pop so a non-bus thread can drain
// what the bus thread enqueued.
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

  int Fileno() const override { return -1; }
  std::vector<Message> TryRead() override { return {}; }

  void Write(const Message& msg) override {
    if (filter_ && msg.stream_id != *filter_) return;
    std::lock_guard<std::mutex> lock(mu_);
    queue_.push_back(msg);
  }

  void Close() override {}

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
