// McapWriterEndpoint — a write-only sink ACTIVE OBJECT that records to MCAP. Send()
// (called on the bus dispatch thread) resolves + snapshots the channel and
// enqueues into a bounded queue (WritePolicy); the endpoint's OWN writer thread
// drains it to disk, so the blocking SD write never touches the bus. Resolution
// happens on the Send() caller (dispatch, serialized) and is snapshotted, so the
// writer thread never touches the (non-thread-safe) ChannelRegistry. Ignores
// on_inbound (write-only). Mirrors python/visio_schema/mcap/writer_endpoint.py.
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <thread>
#include <unordered_map>

#include "visio_schema/mcap/writer.hpp"
#include "visio_schema/routing/channel.hpp"   // Channel
#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/write_policy.hpp"
#include "visio_schema/wire/message.hpp"

namespace visio_schema::mcap {

using visio_schema::wire::Message;

// resolve(stream_id) -> const Channel* (nullptr if unmapped). Called only on the
// Send() caller's thread (the bus dispatch thread).
using StreamResolver = std::function<const Channel*(std::uint32_t)>;

// The writer thread's view of the storage device — its write-stall pattern.
struct McapWriterStats {
  std::uint64_t writes = 0;
  std::uint64_t blocked_ns = 0;
  std::uint64_t max_block_ns = 0;
  std::uint64_t slow_writes = 0;
  // Frames shed by the bounded queue (same count as dropped_frames()) — any
  // nonzero value means the recording is missing data.
  std::uint64_t dropped = 0;
  // Enqueue-side high watermark of queued bytes: how much of the policy's
  // byte bound the storage device's stalls have ever consumed. The distance
  // to max_bytes is the recording's proven margin against loss.
  std::uint64_t max_pending_bytes = 0;
  // Frames discarded because their stream id resolved to no channel. A
  // DIFFERENT failure from `dropped`, and it must not be folded into it: that
  // one means storage is too slow, this one means the recording is missing a
  // whole TOPIC and no amount of faster storage would help. It went uncounted
  // until an Ego Pro recording was found to be silently missing every relayed
  // limb camera while every health field read clean.
  std::uint64_t unmapped = 0;
};

class McapWriterEndpoint : public transport::Endpoint {
 public:
  // rotate_on_keyframe + pair_guard_ns are forwarded to the inner McapWriter; see
  // McapWriter's ctor for their contract (opt-in keyframe-aligned rotation).
  McapWriterEndpoint(std::string_view path, StreamResolver resolve,
                     std::uint64_t max_bytes = 0, double max_duration_s = 0.0,
                     transport::WritePolicy policy = transport::WritePolicy::lossless(),
                     std::map<std::string, std::string> metadata = {},
                     bool rotate_on_keyframe = false, std::int64_t pair_guard_ns = 0,
                     std::uint64_t sync_span_bytes = 0,
                     // Present -> each part is a VREC container instead of
                     // plaintext MCAP. Passthrough; see McapWriter.
                     std::optional<RecordingKey> recording_key = std::nullopt);
  ~McapWriterEndpoint() override;

  McapWriterEndpoint(const McapWriterEndpoint&) = delete;
  McapWriterEndpoint& operator=(const McapWriterEndpoint&) = delete;

  void Start(InboundFn on_inbound, ClosedFn on_closed) override;  // spawn writer thread
  void Send(const Message& msg) override;              // resolve + enqueue
  void Stop() override;                                // stop+join, finalize

  std::size_t pending_frames() const;
  std::size_t pending_bytes() const;
  std::uint64_t dropped_frames() const { return dropped_.load(std::memory_order_relaxed); }
  std::uint64_t unmapped_frames() const {
    return unmapped_.load(std::memory_order_relaxed);
  }
  // Lifetime total of payload bytes written to disk (passthrough to the inner
  // McapWriter; monotonic across part rotation). 0 until the first message drains
  // to the writer thread.
  std::uint64_t bytes_written() const;
  McapWriterStats stats() const;

  // True once the writer thread hit an unrecoverable storage error (card full
  // or flipped read-only). Everything sent afterwards is shed. LATCHED —
  // recording cannot resume on this endpoint; the owner stops and reopens. The
  // reason is logged by the endpoint, not stored (see NoteFailure).
  //
  // Deliberately NOT reported through on_closed: that contract means "fixed
  // link hit EOF, detach me", and a write-only sink ignores both callbacks.
  bool write_failed() const { return failed_.load(std::memory_order_relaxed); }

 private:
  struct Entry {
    std::shared_ptr<const Channel> channel;  // snapshot — writer-thread safe
    Message msg;
  };
  void WriterLoop();
  void DrainBatch(std::deque<Entry>& batch);  // timed writer_->Write
  void NoteDrop(std::size_t n);
  void NoteFailure(const char* what) noexcept;  // latch + log once

  static constexpr std::uint64_t kSlowWriteNs = 50'000'000;  // 50 ms

  const StreamResolver resolve_;
  std::unique_ptr<visio_schema::mcap::McapWriter> writer_;
  transport::WritePolicy policy_;
  // Send()-caller-thread-only (the serialized bus dispatch thread); no lock.
  std::unordered_map<std::uint32_t, std::shared_ptr<const Channel>> channel_cache_;

  mutable std::mutex mu_;       // guards queue_, queue_bytes_, stop_
  std::condition_variable cv_;
  std::deque<Entry> queue_;
  std::size_t queue_bytes_ = 0;
  bool stop_ = false;
  std::thread thread_;

  std::atomic<bool> failed_{false};   // unrecoverable storage error, latched
  std::atomic<std::uint64_t> dropped_{0};
  std::atomic<std::uint64_t> unmapped_{0};
  // Written only under mu_ (Send), read lock-free by stats().
  std::atomic<std::uint64_t> stat_max_pending_bytes_{0};
  std::atomic<std::uint64_t> stat_writes_{0};
  std::atomic<std::uint64_t> stat_blocked_ns_{0};
  std::atomic<std::uint64_t> stat_max_block_ns_{0};
  std::atomic<std::uint64_t> stat_slow_writes_{0};
};

}  // namespace visio_schema::mcap
