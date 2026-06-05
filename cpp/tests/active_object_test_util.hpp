// Test helpers for active-object endpoints (Start/Send/Stop). An endpoint
// delivers inbound from its OWN thread via the on_inbound callback, so tests
// collect into a thread-safe sink and wait on a condition rather than polling.
#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <thread>
#include <vector>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/framing.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/wire/message.hpp"

namespace visio_schema::transport::test {

using visio_schema::wire::Message;

// Collects messages an endpoint delivers from its I/O thread; wait_for() blocks
// until at least n have arrived (or the timeout lapses).
class InboundCollector {
 public:
  Endpoint::InboundFn fn() {
    return [this](Message m, Endpoint*) {
      std::lock_guard<std::mutex> lk(mu_);
      msgs_.push_back(std::move(m));
      cv_.notify_all();
    };
  }
  Endpoint::ClosedFn on_closed() {
    return [this](Endpoint*) {
      std::lock_guard<std::mutex> lk(mu_);
      closed_ = true;
      cv_.notify_all();
    };
  }
  std::size_t wait_for(std::size_t n, std::chrono::milliseconds timeout =
                                          std::chrono::seconds(2)) {
    std::unique_lock<std::mutex> lk(mu_);
    cv_.wait_for(lk, timeout, [&] { return msgs_.size() >= n; });
    return msgs_.size();
  }
  bool wait_closed(std::chrono::milliseconds timeout = std::chrono::seconds(2)) {
    std::unique_lock<std::mutex> lk(mu_);
    return cv_.wait_for(lk, timeout, [&] { return closed_; });
  }
  std::vector<Message> messages() const {
    std::lock_guard<std::mutex> lk(mu_);
    return msgs_;
  }
  std::size_t size() const {
    std::lock_guard<std::mutex> lk(mu_);
    return msgs_.size();
  }
  bool closed() const {
    std::lock_guard<std::mutex> lk(mu_);
    return closed_;
  }

 private:
  mutable std::mutex mu_;
  std::condition_variable cv_;
  std::vector<Message> msgs_;
  bool closed_ = false;
};

// Read framed messages directly off a (peer) fd until n arrive or timeout — for
// asserting what an endpoint's Send() actually put on the wire.
inline std::vector<Message> ReadFramesFromFd(
    int fd, std::size_t n,
    std::chrono::milliseconds timeout = std::chrono::seconds(2)) {
  std::vector<std::uint8_t> rx;
  std::vector<Message> out;
  const auto deadline = std::chrono::steady_clock::now() + timeout;
  while (out.size() < n && std::chrono::steady_clock::now() < deadline) {
    std::uint8_t buf[4096];
    const long r = ReadSome(fd, buf, sizeof(buf));
    if (r > 0) {
      rx.insert(rx.end(), buf, buf + r);
      for (auto& m : ExtractFrames(rx)) out.push_back(std::move(m));
    } else if (r < 0) {
      break;  // EOF
    } else {
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
  }
  return out;
}

}  // namespace visio_schema::transport::test
