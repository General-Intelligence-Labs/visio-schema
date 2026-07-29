// FramedOutbox — bounded outbound queue + non-blocking drain for one streaming
// sink. A port of an earlier channel implementation's write path (enqueue_locked_
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
#include <memory>
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

  // A framed buffer as the queue holds it: shared and immutable, so one
  // EncodeFramed result fans out to N sinks' outboxes without N copies.
  using SharedFrame = std::shared_ptr<const std::vector<std::uint8_t>>;

  // Queue one already-framed payload. Applies the WritePolicy; never blocks.
  // Thread-safe. Returns false only when DropOnFail rejected this frame.
  // The SharedFrame overload is the cheap path (refcount, no copy); the raw
  // pointer overload copies and wraps for callers without a shared buffer.
  bool Enqueue(SharedFrame frame, bool keyframe = false);
  bool Enqueue(const std::uint8_t* frame, std::size_t len,
               bool keyframe = false);

  // True iff a frame is committed to the wire but not yet fully written (a
  // partial in_flight_). The owning endpoint uses this to multiplex two outboxes
  // (control + bulk) over one fd WITHOUT interleaving half-frames: it only
  // switches queues when neither has bytes mid-flight. Leg-thread-local read.
  bool InFlightActive() const { return in_flight_off_ < in_flight_size(); }

  // Drain as much as the link will accept right now, non-blocking. Returns false
  // if `wr` reported the link dead (<0). Call ONLY from the single leg I/O thread.
  bool Drain(const WriteFn& wr);

  // True while there are committed-but-unwritten bytes or queued frames — i.e.
  // the sink wants POLLOUT. (in_flight_ read is leg-thread-local.)
  bool HasPending() const {
    if (in_flight_off_ < in_flight_size()) return true;
    std::lock_guard<std::mutex> lk(mu_);
    return !queue_.empty();
  }
  // Total bytes the outbox is holding (in-flight remainder + queued).
  std::size_t PendingBytes() const {
    const std::size_t inflight = in_flight_size() - in_flight_off_;
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
    SharedFrame data;  // shared with sibling sinks; eviction drops a refcount
    std::int64_t enqueue_us;
    bool keyframe = false;  // never age-evicted: it is the decoder's sync point
  };
  void PromoteToInFlight();  // queue_ -> in_flight_, per DrainMode (caller holds mu_)
  std::size_t in_flight_size() const {
    return in_flight_ ? in_flight_->size() : 0;
  }

  mutable std::mutex mu_;                  // guards queue_, queue_bytes_
  WritePolicy policy_;
  NowFn now_;
  std::deque<Entry> queue_;
  std::size_t queue_bytes_ = 0;
  // Drainer-private (one leg thread). OneAtATime promotion is a refcount move
  // of the shared entry; BatchAll wraps its concatenation scratch — either
  // way the committed bytes are immutable and never evicted.
  SharedFrame in_flight_;
  std::size_t in_flight_off_ = 0;
  std::atomic<std::uint64_t> dropped_{0};
};

}  // namespace visio_schema::transport
