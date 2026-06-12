#include "visio_schema/transport/framed_outbox.hpp"

#include <chrono>

namespace visio_schema::transport {

std::int64_t FramedOutbox::SteadyNowUs() {
  return std::chrono::duration_cast<std::chrono::microseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

FramedOutbox::FramedOutbox(WritePolicy policy, NowFn now)
    : policy_(policy), now_(std::move(now)) {}

bool FramedOutbox::Enqueue(const std::uint8_t* frame, std::size_t len) {
  const std::int64_t enqueue_us = now_();
  std::lock_guard<std::mutex> lk(mu_);
  const std::size_t before = queue_.size();
  if (!ApplyDropBound(policy_, queue_, queue_bytes_, len,
                      [](const Entry& e) { return e.data.size(); })) {
    dropped_.fetch_add(1, std::memory_order_relaxed);  // DropOnFail: rejected
    return false;
  }
  if (const std::size_t evicted = before - queue_.size()) {
    dropped_.fetch_add(evicted, std::memory_order_relaxed);  // evicted-oldest
  }
  queue_.push_back({std::vector<std::uint8_t>(frame, frame + len), enqueue_us});
  queue_bytes_ += len;
  return true;
}

// Three-phase drain (see header + umi_channel.hpp). The blocking WriteFn runs
// OUTSIDE mu_; only phase 2/3 (touching queue_) hold the lock. Eviction may only
// touch the uncommitted queue_; in_flight_ is already on the wire.
bool FramedOutbox::Drain(const WriteFn& wr) {
  // Phase 1: finish what's in-flight (drainer-private; no lock). Committed bytes.
  if (in_flight_off_ < in_flight_.size()) {
    const long r = wr(in_flight_.data() + in_flight_off_,
                      in_flight_.size() - in_flight_off_);
    if (r < 0) return false;  // link dead
    in_flight_off_ += static_cast<std::size_t>(r);
    if (in_flight_off_ < in_flight_.size()) return true;  // partial/EAGAIN
    in_flight_.clear();
    in_flight_off_ = 0;
  }

  // Phase 2 + 3 under the queue lock: age-evict, then promote into in_flight_.
  {
    std::lock_guard<std::mutex> lk(mu_);
    if (policy_.drop == WritePolicy::DropMode::StaleEviction &&
        policy_.max_age.count() > 0) {
      const std::int64_t now_us = now_();
      const std::int64_t max_age_us = policy_.max_age.count();
      // Evict stale frames, but keep small control frames (< protect_below_bytes)
      // even when stale — on a leg flooded with bulk video they age out behind it,
      // yet the host still needs them (command results, OTA status, DeviceInfo).
      // With protect_below_bytes==0 this is the original front-run shed: frames
      // are FIFO/time-ordered, so the stale run is contiguous at the front.
      for (auto it = queue_.begin(); it != queue_.end();) {
        if ((now_us - it->enqueue_us) <= max_age_us) {
          ++it;
        } else if (policy_.protect_below_bytes > 0 &&
                   it->data.size() < policy_.protect_below_bytes) {
          ++it;  // keep this stale-but-small control frame
        } else {
          queue_bytes_ -= it->data.size();
          it = queue_.erase(it);
        }
      }
    }
    if (queue_.empty()) return true;
    PromoteToInFlight();  // queue_ -> in_flight_ (commit point)
  }

  // Trailing best-effort write (no lock; in_flight_ is drainer-private now).
  if (in_flight_off_ < in_flight_.size()) {
    const long r = wr(in_flight_.data() + in_flight_off_,
                      in_flight_.size() - in_flight_off_);
    if (r < 0) return false;
    in_flight_off_ += static_cast<std::size_t>(r);
    if (in_flight_off_ == in_flight_.size()) {
      in_flight_.clear();
      in_flight_off_ = 0;
    }
  }
  return true;
}

// Caller holds mu_.
void FramedOutbox::PromoteToInFlight() {
  if (queue_.empty()) return;
  if (policy_.drain == WritePolicy::DrainMode::BatchAll) {
    std::size_t total = 0;
    for (const auto& e : queue_) total += e.data.size();
    in_flight_.clear();
    in_flight_.reserve(total);
    for (auto& e : queue_) {
      in_flight_.insert(in_flight_.end(), e.data.begin(), e.data.end());
    }
    queue_.clear();
    queue_bytes_ = 0;
  } else {  // OneAtATime
    in_flight_ = std::move(queue_.front().data);
    queue_bytes_ -= in_flight_.size();
    queue_.pop_front();
  }
  in_flight_off_ = 0;
}

void FramedOutbox::Clear() {
  {
    std::lock_guard<std::mutex> lk(mu_);
    queue_.clear();
    queue_bytes_ = 0;
  }
  in_flight_.clear();   // drainer-private
  in_flight_off_ = 0;
}

}  // namespace visio_schema::transport
