// MessageInbox — a bounded, thread-safe inbound queue of decoded Messages with a
// condition variable so a single consumer can block until frames arrive. The
// inbound twin of FramedOutbox: outbound decouples a producer from a slow link;
// inbound decouples an endpoint's reader thread from a slow consumer (in the
// Python binding, a GIL-bound render thread). Backpressure reuses the shared
// WritePolicy / ApplyDropBound machinery — drop-oldest by default, a Dropped()
// counter — exactly as FramedOutbox (byte frames) and McapEndpoint (Messages)
// already do; the per-frame dropped accounting mirrors FramedOutbox::Enqueue.
//
// Threading: one producer (Push — an endpoint's reader thread) and one consumer
// (PopBatch / Close). Push never blocks; PopBatch blocks up to a timeout. Both
// are pure C++ with no Python types, so the binding calls PopBatch with the GIL
// released — which is what keeps the reader thread off the GIL.
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <iterator>
#include <mutex>
#include <vector>

#include "visio_schema/transport/write_policy.hpp"
#include "visio_schema/wire/message.hpp"

namespace visio_schema::transport {

class MessageInbox {
 public:
  explicit MessageInbox(WritePolicy policy = WritePolicy::drop_oldest())
      : policy_(policy) {}

  // Producer: enqueue one Message (moved in). Applies the WritePolicy; never
  // blocks. Wakes a waiting consumer.
  void Push(wire::Message&& msg) {
    {
      std::lock_guard<std::mutex> lk(mu_);
      const std::size_t incoming = msg.payload.size();
      const std::size_t before = queue_.size();
      if (!ApplyDropBound(policy_, queue_, bytes_, incoming,
                          [](const wire::Message& m) { return m.payload.size(); })) {
        dropped_.fetch_add(1, std::memory_order_relaxed);  // DropOnFail: rejected
        return;
      }
      if (const std::size_t evicted = before - queue_.size()) {
        dropped_.fetch_add(evicted, std::memory_order_relaxed);  // evicted-oldest
      }
      queue_.push_back(std::move(msg));
      bytes_ += incoming;
    }
    cv_.notify_one();
  }

  // Consumer: wait up to `timeout_ms` for frames, then take up to `max_frames`
  // (0 = no cap). Returns the batch (empty on timeout/closed-empty). Pure C++ —
  // the binding holds the GIL released across this call. The common no-cap case
  // hands off the whole deque under the lock in O(1), so the producer's Push is
  // never blocked for the O(N) move-out.
  std::vector<wire::Message> PopBatch(int timeout_ms, std::size_t max_frames) {
    std::deque<wire::Message> taken;
    {
      std::unique_lock<std::mutex> lk(mu_);
      if (queue_.empty() && !closed_) {
        cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms),
                     [this] { return !queue_.empty() || closed_; });
      }
      if (max_frames == 0 || max_frames >= queue_.size()) {
        taken.swap(queue_);
        bytes_ = 0;
      } else {
        for (std::size_t i = 0; i < max_frames; ++i) {
          bytes_ -= queue_.front().payload.size();
          taken.push_back(std::move(queue_.front()));
          queue_.pop_front();
        }
      }
    }
    return {std::make_move_iterator(taken.begin()),
            std::make_move_iterator(taken.end())};
  }

  // Wake any waiter and mark closed; subsequent PopBatch waits return at once.
  void Close() {
    {
      std::lock_guard<std::mutex> lk(mu_);
      closed_ = true;
    }
    cv_.notify_all();
  }

  bool closed() const {
    std::lock_guard<std::mutex> lk(mu_);
    return closed_;
  }
  std::uint64_t Dropped() const { return dropped_.load(std::memory_order_relaxed); }
  std::size_t size() const {
    std::lock_guard<std::mutex> lk(mu_);
    return queue_.size();
  }

 private:
  mutable std::mutex mu_;
  std::condition_variable cv_;
  WritePolicy policy_;
  std::deque<wire::Message> queue_;
  std::size_t bytes_ = 0;
  bool closed_ = false;
  std::atomic<std::uint64_t> dropped_{0};
};

}  // namespace visio_schema::transport
