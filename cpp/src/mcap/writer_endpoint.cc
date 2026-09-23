#include "visio_schema/mcap/writer_endpoint.hpp"

#include <chrono>
#include <utility>

#include "visio_schema/log.hpp"
#include "visio_schema/transport/link.hpp"  // EnterServiceThread

namespace visio_schema::mcap {

namespace {
std::uint64_t SteadyNs() {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}
}  // namespace

McapWriterEndpoint::McapWriterEndpoint(std::string_view path, StreamResolver resolve,
                                       std::uint64_t max_bytes, double max_duration_s,
                                       transport::WritePolicy policy,
                                       std::map<std::string, std::string> metadata,
                                       bool rotate_on_keyframe, std::int64_t pair_guard_ns,
                                       std::uint64_t sync_span_bytes,
                                       std::optional<RecordingKey> recording_key,
                                       McapReadbackOptions readback)
    : resolve_(std::move(resolve)),
      writer_(std::make_unique<visio_schema::mcap::McapWriter>(
          path, max_bytes, max_duration_s, rotate_on_keyframe, pair_guard_ns,
          sync_span_bytes, std::move(recording_key), std::move(readback))),
      policy_(policy) {
  // Written on this (constructing) thread, before Start() spawns the writer
  // thread — so it lands in the file ahead of any message, no locking needed.
  if (!metadata.empty()) writer_->SetMetadata("visio.capture", std::move(metadata));
}

McapWriterEndpoint::~McapWriterEndpoint() { Stop(); }

void McapWriterEndpoint::Start(InboundFn /*on_inbound*/, ClosedFn /*on_closed*/) {
  {
    std::lock_guard<std::mutex> lk(mu_);
    stop_ = false;
  }
  if (!thread_.joinable()) thread_ = std::thread([this] { WriterLoop(); });
}

void McapWriterEndpoint::Stop() {
  {
    std::lock_guard<std::mutex> lk(mu_);
    if (stop_) return;
    stop_ = true;
  }
  cv_.notify_one();
  if (thread_.joinable()) thread_.join();  // drains the remaining queue
  // Close() finalizes the part against the same storage that may have just
  // died, and ~McapWriterEndpoint calls Stop() — a destructor is noexcept, so
  // anything escaping here terminates the process during teardown. Catch(...)
  // rather than std::exception: this is the last frame before noexcept, and
  // NoteFailure is nothrow, so nothing can get past it. Losing the footer costs
  // one part's index; the uploader's torn-part repair recovers it.
  if (writer_) {
    try {
      writer_->Close();
    } catch (...) {
      NoteFailure("finalize failed");
    }
  }
}

bool McapWriterEndpoint::CrossedLogThreshold(std::uint64_t prev,
                                             std::size_t n) {
  return prev == 0 || (prev + n) / 1000 != prev / 1000;
}

void McapWriterEndpoint::NoteDrop(std::size_t n) {
  const std::uint64_t prev = dropped_.fetch_add(n, std::memory_order_relaxed);
  if (CrossedLogThreshold(prev, n)) {
    log::Write(
        log::Severity::kWarning,
        "McapWriterEndpoint: dropped %llu frames (storage can't keep up with "
        "the recording)",
        static_cast<unsigned long long>(prev + n));
  }
}

// Counted and reported, because the failure it hides is total: an id that never
// maps loses its ENTIRE topic for the whole recording, and unlike a queue drop
// no amount of faster storage helps. Deliberately NOT folded into `dropped` —
// that one means storage is too slow, this one means a topic is missing.
//
// The id is not named: this counter is global to the endpoint, so with two
// unmapped ids interleaving the message would name whichever arrived first and
// never mention the second. A count is honest; a misleading id is not.
void McapWriterEndpoint::NoteUnmapped(std::uint32_t) {
  const std::uint64_t prev = unmapped_.fetch_add(1, std::memory_order_relaxed);
  if (CrossedLogThreshold(prev, 1)) {
    log::Write(
        log::Severity::kWarning,
        "McapWriterEndpoint: %llu frames resolve to no channel — at least one "
        "topic is absent from this recording",
        static_cast<unsigned long long>(prev + 1));
  }
}

void McapWriterEndpoint::Send(const Message& msg) {
  // Resolve on the caller (bus dispatch) thread; snapshot the Channel once per id
  // so the writer thread is independent of the live registry.
  std::shared_ptr<const Channel> ch;
  if (auto it = channel_cache_.find(msg.stream_id); it != channel_cache_.end()) {
    ch = it->second;
  } else {
    const Channel* resolved = resolve_ ? resolve_(msg.stream_id) : nullptr;
    if (resolved == nullptr) {
      NoteUnmapped(msg.stream_id);  // drop-until-mapped
      return;
    }
    ch = std::make_shared<const Channel>(*resolved);
    channel_cache_.emplace(msg.stream_id, ch);
  }

  const std::size_t len = msg.payload.size();
  bool was_empty = false;
  {
    std::lock_guard<std::mutex> lk(mu_);
    const std::size_t before = queue_.size();
    if (!transport::ApplyDropBound(policy_, queue_, queue_bytes_, len,
                                   [](const Entry& e) { return e.msg.payload.size(); })) {
      NoteDrop(1);
      return;
    }
    if (const std::size_t evicted = before - queue_.size()) NoteDrop(evicted);
    queue_.push_back(Entry{std::move(ch), msg});
    queue_bytes_ += len;
    // The QUEUE's high-water mark, deliberately not queue+in-flight: this stat
    // is the queue's relationship to the policy bound (the firmware prints it
    // beside the drop count), and folding in a batch the writer thread happens
    // to be holding would make it depend on writer timing — it would read up to
    // 2x the bound on a healthy board. Peak RESIDENCY is a different question
    // and pending_bytes() is where it is answered honestly.
    if (queue_bytes_ > stat_max_pending_bytes_.load(std::memory_order_relaxed))
      stat_max_pending_bytes_.store(queue_bytes_, std::memory_order_relaxed);
    was_empty = before == 0;
  }
  // Signal only the empty→non-empty edge: the writer swaps the WHOLE queue
  // per wake and its wait predicate re-checks emptiness under the lock, so
  // while the queue is non-empty it is either draining or about to re-check —
  // never blocked. Per-message notify_one was ~590 futex signals/s while
  // recording, for a writer that wakes a handful of times a second.
  if (was_empty) cv_.notify_one();
}

void McapWriterEndpoint::WriterLoop() {
  // Without a name this thread inherits its creator's comm (on-device that is
  // the command worker's), which mis-attributes all recording CPU in top -H.
  transport::EnterServiceThread("mcap_wr", 0);
  for (;;) {
    std::deque<Entry> batch;
    {
      std::unique_lock<std::mutex> lk(mu_);
      cv_.wait(lk, [this] { return stop_ || !queue_.empty(); });
      batch.swap(queue_);
      // The bytes just taken are exactly what the queue was holding; they stay
      // counted (as in-flight) until each one is written and freed, so
      // pending_bytes() never pretends the endpoint got lighter at the swap.
      inflight_bytes_.store(queue_bytes_, std::memory_order_relaxed);
      queue_bytes_ = 0;
      if (batch.empty() && stop_) return;  // stopped + fully drained
    }
    // This is the thread's ENTRY function: an exception leaving it calls
    // std::terminate and takes the whole process down. McapWriter throws when
    // it cannot open the next part — a full card, or one the kernel flipped
    // read-only under us — so latch that instead and keep the thread alive to
    // shed what is queued, leaving Send()/Stop() well-behaved. The owner polls
    // write_failed() and stops the recording.
    if (failed_.load(std::memory_order_relaxed)) {
      NoteDrop(batch.size());
    } else {
      try {
        DrainBatch(batch);
      } catch (const std::exception& e) {
        NoteFailure(e.what());
      } catch (...) {   // nothing may escape this frame; see above
        NoteFailure("unknown exception");
      }
    }
    // One release point for every path out of the batch (drained, shed by the
    // latch, or abandoned mid-write): free whatever it still holds FIRST, then
    // zero the accounting, so the gauge never reads lighter than the process is.
    batch.clear();
    inflight_bytes_.store(0, std::memory_order_relaxed);
  }
}

// nothrow: also reached from Stop(), which a noexcept destructor calls. The
// reason is logged here and nowhere else — deliberately not stored, so no
// allocation sits on the teardown path (a card dying and the heap failing are
// the same bad day) and callers need only the latch.
void McapWriterEndpoint::NoteFailure(const char* what) noexcept {
  if (!failed_.exchange(true, std::memory_order_relaxed)) {
    log::Write(
        log::Severity::kError,
        "McapWriterEndpoint: recording stopped — storage write failed: %s",
        what);
  }
}

// POP as we go, rather than iterating and clearing at the end: an entry's
// payload is released the instant it has been written, so the batch's resident
// bytes fall through the drain. Holding all of them to a trailing clear() made
// the queue's byte bound effectively DOUBLE — Send() may refill the queue to
// the full bound (16 MiB on the device) while the batch just swapped out is
// still resident, and the COBS `framed` cache each Message may carry from the
// fanout (wire/message.hpp) rides along on top, uncounted. On a 44 MB box that
// was the difference between a bound and a wish.
void McapWriterEndpoint::DrainBatch(std::deque<Entry>& batch) {
  while (!batch.empty()) {
    const Entry& e = batch.front();
    const std::size_t bytes = e.msg.payload.size();
    if (e.channel) {
      const std::uint64_t t0 = SteadyNs();
      writer_->Write(*e.channel, e.msg);
      const std::uint64_t dt = SteadyNs() - t0;
      stat_writes_.fetch_add(1, std::memory_order_relaxed);
      stat_blocked_ns_.fetch_add(dt, std::memory_order_relaxed);
      std::uint64_t cur = stat_max_block_ns_.load(std::memory_order_relaxed);
      while (dt > cur && !stat_max_block_ns_.compare_exchange_weak(
                             cur, dt, std::memory_order_relaxed)) {
      }
      if (dt > kSlowWriteNs) stat_slow_writes_.fetch_add(1, std::memory_order_relaxed);
    }
    batch.pop_front();  // frees this payload HERE, not at the end of the batch
    inflight_bytes_.fetch_sub(bytes, std::memory_order_relaxed);
  }
}

std::size_t McapWriterEndpoint::pending_frames() const {
  std::lock_guard<std::mutex> lk(mu_);
  return queue_.size();
}

std::size_t McapWriterEndpoint::pending_bytes() const {
  std::lock_guard<std::mutex> lk(mu_);
  return queue_bytes_ +
         static_cast<std::size_t>(
             inflight_bytes_.load(std::memory_order_relaxed));
}

std::uint64_t McapWriterEndpoint::bytes_written() const {
  // writer_ is created in the ctor and only Close()d (never reset) in Stop(),
  // so it stays valid for the endpoint's lifetime; bytes_written() reads an
  // atomic, so polling it from another thread needs no lock.
  return writer_ ? writer_->bytes_written() : 0;
}

// writer_ outlives the endpoint (see bytes_written); the read-back's own
// lock serializes a step against Close, so no endpoint lock is needed.
bool McapWriterEndpoint::ReadbackStep(std::chrono::milliseconds budget) {
  return writer_ && writer_->ReadbackStep(budget);
}

std::size_t McapWriterEndpoint::readback_pending() const {
  return writer_ ? writer_->readback_pending() : 0;
}

McapReadbackStats McapWriterEndpoint::readback_stats() const {
  return writer_ ? writer_->readback_stats() : McapReadbackStats{};
}

bool McapWriterEndpoint::storage_fault() const {
  return writer_ && writer_->storage_fault();
}

McapWriterStats McapWriterEndpoint::stats() const {
  McapWriterStats s;
  s.writes = stat_writes_.load(std::memory_order_relaxed);
  s.blocked_ns = stat_blocked_ns_.load(std::memory_order_relaxed);
  s.max_block_ns = stat_max_block_ns_.load(std::memory_order_relaxed);
  s.slow_writes = stat_slow_writes_.load(std::memory_order_relaxed);
  s.dropped = dropped_.load(std::memory_order_relaxed);
  s.unmapped = unmapped_.load(std::memory_order_relaxed);
  s.max_pending_bytes = stat_max_pending_bytes_.load(std::memory_order_relaxed);
  return s;
}

}  // namespace visio_schema::mcap
