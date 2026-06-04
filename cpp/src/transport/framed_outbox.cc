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
  if (!ApplyDropBound(policy_, queue_, queue_bytes_, len,
                      [](const Entry& e) { return e.data.size(); })) {
    return false;  // DropOnFail: queue at max_depth
  }
  queue_.push_back({std::vector<std::uint8_t>(frame, frame + len), enqueue_us});
  queue_bytes_ += len;
  return true;
}

// Three-phase drain (see header + umi_channel.hpp). Eviction (phase 2) may only
// touch the uncommitted queue_; in_flight_ is already on the wire.
bool FramedOutbox::Drain(const WriteFn& wr) {
  // Phase 1: finish what's in-flight. No eviction here — committed bytes.
  if (in_flight_off_ < in_flight_.size()) {
    const long r = wr(in_flight_.data() + in_flight_off_,
                      in_flight_.size() - in_flight_off_);
    if (r < 0) return false;  // link dead
    in_flight_off_ += static_cast<std::size_t>(r);
    if (in_flight_off_ < in_flight_.size()) return true;  // partial/EAGAIN
    in_flight_.clear();
    in_flight_off_ = 0;
  }

  // Phase 2: queue is the only source of pending bytes now. Age-evict.
  if (policy_.drop == WritePolicy::DropMode::StaleEviction &&
      policy_.max_age.count() > 0) {
    const std::int64_t now_us = now_();
    const std::int64_t max_age_us = policy_.max_age.count();
    while (!queue_.empty() &&
           (now_us - queue_.front().enqueue_us) > max_age_us) {
      queue_bytes_ -= queue_.front().data.size();
      queue_.pop_front();
    }
  }
  if (queue_.empty()) return true;

  // Phase 3: promote next batch/frame, then best-effort write it this tick.
  PromoteToInFlight();
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
  queue_.clear();
  queue_bytes_ = 0;
  in_flight_.clear();
  in_flight_off_ = 0;
}

}  // namespace visio_schema::transport
