// FramedOutbox — bounded outbound queue + non-blocking drain for one streaming
// sink. A port of umi_protocol/cpp/umi_channel.hpp's write path (enqueue_locked_
// / drain_writes_ / promote_to_in_flight_locked_). It is transport-neutral — the
// owning Endpoint supplies a non-blocking WriteFn over its fd.
//
// Thread-safety (per-leg-thread bus): Enqueue() runs on the producer/dispatch
// thread; Drain() runs on the leg's own I/O thread. The queue (queue_/queue_bytes_)
// is guarded by mu_; `in_flight_` is touched ONLY by the single draining leg
// thread (Drain/Clear), so it needs no lock. The blocking WriteFn is invoked
// OUTSIDE mu_ — a slow/stalled write never blocks an Enqueue.
//
// The hard correctness property it preserves (the original's H.265-corruption
// fix): once bytes are promoted into `in_flight_` they are committed to the wire
// and eviction must NEVER touch them — only the uncommitted `queue_` is shed.
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <vector>

#include "visio_schema/transport/write_policy.hpp"

namespace visio_schema::transport {

class FramedOutbox {
 public:
  // Non-blocking write of one buffer. Returns bytes accepted (0..len), 0 on
  // would-block (EAGAIN), or <0 if the link is dead. Mirrors ::write semantics.
  using WriteFn = std::function<long(const std::uint8_t*, std::size_t)>;
  // Monotonic clock in microseconds (injected so tests don't touch the clock).
  using NowFn = std::function<std::int64_t()>;

  explicit FramedOutbox(WritePolicy policy, NowFn now = SteadyNowUs);

  // Queue one already-framed payload. Applies the WritePolicy; never blocks.
  // Thread-safe. Returns false only when DropOnFail rejected this frame.
  bool Enqueue(const std::uint8_t* frame, std::size_t len);

  // True iff a frame is committed to the wire but not yet fully written (a
  // partial in_flight_). The owning endpoint uses this to multiplex two outboxes
  // (control + bulk) over one fd WITHOUT interleaving half-frames: it only
  // switches queues when neither has bytes mid-flight. Leg-thread-local read.
  bool InFlightActive() const { return in_flight_off_ < in_flight_.size(); }

  // Drain as much as the link will accept right now, non-blocking. Returns false
  // if `wr` reported the link dead (<0). Call ONLY from the single leg I/O thread.
  bool Drain(const WriteFn& wr);

  // True while there are committed-but-unwritten bytes or queued frames — i.e.
  // the sink wants POLLOUT. (in_flight_ read is leg-thread-local.)
  bool HasPending() const {
    if (in_flight_off_ < in_flight_.size()) return true;
    std::lock_guard<std::mutex> lk(mu_);
    return !queue_.empty();
  }
  // Total bytes the outbox is holding (in-flight remainder + queued).
  std::size_t PendingBytes() const {
    const std::size_t inflight = in_flight_.size() - in_flight_off_;
    std::lock_guard<std::mutex> lk(mu_);
    return inflight + queue_bytes_;
  }
  std::size_t QueuedFrames() const {
    std::lock_guard<std::mutex> lk(mu_);
    return queue_.size();
  }
  // Cumulative frames shed by the WritePolicy (evicted-oldest or rejected).
  std::uint64_t Dropped() const { return dropped_.load(std::memory_order_relaxed); }

  // Drop everything (e.g. on link reopen — a fresh reader would desync on stale
  // committed bytes). Call from the leg thread.
  void Clear();

  static std::int64_t SteadyNowUs();

 private:
  struct Entry {
    std::vector<std::uint8_t> data;
    std::int64_t enqueue_us;
  };
  void PromoteToInFlight();  // queue_ -> in_flight_, per DrainMode (caller holds mu_)

  mutable std::mutex mu_;                  // guards queue_, queue_bytes_
  WritePolicy policy_;
  NowFn now_;
  std::deque<Entry> queue_;
  std::size_t queue_bytes_ = 0;
  std::vector<std::uint8_t> in_flight_;    // drainer-private (one leg thread)
  std::size_t in_flight_off_ = 0;          // drainer-private
  std::atomic<std::uint64_t> dropped_{0};
};

}  // namespace visio_schema::transport
