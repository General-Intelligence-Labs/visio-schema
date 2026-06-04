// FramedOutbox — bounded outbound queue + non-blocking drain for one streaming
// sink. A single-threaded port of umi_protocol/cpp/umi_channel.hpp's write path
// (enqueue_locked_ / drain_writes_ / promote_to_in_flight_locked_), minus the
// mutexes: on the Visio bus only the reactor thread ever touches an outbox, so
// no locking is needed. It is transport-neutral — the owning Endpoint supplies a
// non-blocking WriteFn over its fd, and the bus reactor calls Drain() whenever
// the fd reports POLLOUT.
//
// The hard correctness property it preserves (the original's H.265-corruption
// fix): once bytes are promoted into `in_flight_` they are committed to the wire
// and eviction must NEVER touch them — only the uncommitted `queue_` is shed.
// Dropping a partially-written frame mid-stream corrupts the peer's decoder.
#pragma once

#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
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

  // Queue one already-framed payload (COBS frame, header, whatever the sink
  // sends). Applies the WritePolicy; never blocks. Returns false only when
  // DropOnFail rejected this frame because the queue was at max_depth.
  bool Enqueue(const std::uint8_t* frame, std::size_t len);

  // Drain as much as the link will accept right now, non-blocking. Returns
  // false if `wr` reported the link dead (<0) — the caller marks the link down.
  // True otherwise (idle, partial, or fully drained).
  bool Drain(const WriteFn& wr);

  // True while there are committed-but-unwritten bytes or queued frames — i.e.
  // the sink wants POLLOUT.
  bool HasPending() const {
    return in_flight_off_ < in_flight_.size() || !queue_.empty();
  }
  // Total bytes the outbox is holding (in-flight remainder + queued). The
  // serial watchdog reads this as the "is the host draining us" signal.
  std::size_t PendingBytes() const {
    return (in_flight_.size() - in_flight_off_) + queue_bytes_;
  }
  std::size_t QueuedFrames() const { return queue_.size(); }

  // Drop everything (e.g. on link reopen — the peer is a fresh reader, so stale
  // committed bytes would desync its framer).
  void Clear();

  static std::int64_t SteadyNowUs();

 private:
  struct Entry {
    std::vector<std::uint8_t> data;
    std::int64_t enqueue_us;
  };
  void PromoteToInFlight();  // queue_ -> in_flight_, per DrainMode

  WritePolicy policy_;
  NowFn now_;
  std::deque<Entry> queue_;
  std::size_t queue_bytes_ = 0;
  std::vector<std::uint8_t> in_flight_;
  std::size_t in_flight_off_ = 0;
};

}  // namespace visio_schema::transport
