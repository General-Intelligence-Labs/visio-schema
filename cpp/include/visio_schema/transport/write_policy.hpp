// WritePolicy — bounded outbound-queue policy for a streaming sink. Ported from
// the original umi_protocol/cpp/umi_channel.hpp::WritePolicy so the embedded
// backpressure behavior matches what shipped: a slow/stalled consumer never
// blocks producers; the queue sheds frames per the chosen mode.
#pragma once

#include <chrono>
#include <climits>
#include <cstddef>

namespace visio_schema::transport {

struct WritePolicy {
  // How the queue sheds when it can't drain fast enough.
  enum class DropMode {
    DropOldest,      // at max_depth, pop the oldest queued frame (keep freshest)
    DropOnFail,      // at max_depth, reject the NEW frame (Enqueue returns false)
    StaleEviction,   // bound by max_bytes; also drop frames older than max_age
  };
  // How many queued frames are promoted to the wire per drain pass.
  enum class DrainMode {
    OneAtATime,      // one frame per writable tick
    BatchAll,        // coalesce all queued frames into one write (fewer syscalls)
  };

  DropMode  drop  = DropMode::DropOldest;
  DrainMode drain = DrainMode::OneAtATime;
  std::size_t max_depth = 1024;          // entries (DropOldest / DropOnFail)
  std::size_t max_bytes = SIZE_MAX;      // byte cap (StaleEviction)
  std::chrono::microseconds max_age{0};  // 0 = no age cap (StaleEviction)

  static WritePolicy drop_oldest(std::size_t depth = 1024) {
    WritePolicy p; p.drop = DropMode::DropOldest; p.max_depth = depth; return p;
  }
  static WritePolicy drop_on_fail(std::size_t depth = 1) {
    WritePolicy p; p.drop = DropMode::DropOnFail; p.max_depth = depth; return p;
  }
  // Bound RAM by bytes and drop frames older than `age` — the original serial
  // leg's policy; right for real-time streams on tight memory.
  static WritePolicy stale_eviction(std::size_t max_bytes,
                                    std::chrono::microseconds age,
                                    DrainMode dm = DrainMode::BatchAll) {
    WritePolicy p; p.drop = DropMode::StaleEviction; p.drain = dm;
    p.max_bytes = max_bytes; p.max_age = age; return p;
  }
  // Never drop (host / Python recorder). Unbounded queue — only for consumers
  // that always keep up or where loss is unacceptable and RAM is ample.
  static WritePolicy lossless() {
    WritePolicy p; p.drop = DropMode::DropOldest; p.max_depth = SIZE_MAX; return p;
  }
};

// Evict oldest entries from a bounded FIFO `q` so one more frame of `incoming`
// bytes fits, per `policy`. `bytes` is the queue's running byte total (updated
// in place); `size_of(q.front())` returns an entry's byte size. Returns false
// iff DropOnFail rejects the new frame (the caller must not enqueue it). Shared
// by FramedOutbox (byte frames) and McapEndpoint (Messages) so the count/byte
// drop semantics live in one place. Age-based eviction (max_age) is applied
// separately at drain time by callers that timestamp their entries.
template <class Queue, class SizeOf>
bool ApplyDropBound(const WritePolicy& policy, Queue& q, std::size_t& bytes,
                    std::size_t incoming, SizeOf size_of) {
  switch (policy.drop) {
    case WritePolicy::DropMode::DropOldest:
      while (q.size() >= policy.max_depth && !q.empty()) {
        bytes -= size_of(q.front());
        q.pop_front();
      }
      return true;
    case WritePolicy::DropMode::StaleEviction:
      while ((bytes + incoming) > policy.max_bytes && !q.empty()) {
        bytes -= size_of(q.front());
        q.pop_front();
      }
      return true;
    case WritePolicy::DropMode::DropOnFail:
      return q.size() < policy.max_depth;
  }
  return true;
}

}  // namespace visio_schema::transport
