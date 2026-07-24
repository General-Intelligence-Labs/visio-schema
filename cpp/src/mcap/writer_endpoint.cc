#include "visio_schema/mcap/writer_endpoint.hpp"

#include <chrono>
#include <iostream>
#include <utility>

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
                                       std::map<std::string, std::string> metadata)
    : resolve_(std::move(resolve)),
      writer_(std::make_unique<visio_schema::mcap::McapWriter>(
          path, max_bytes, max_duration_s)),
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

void McapWriterEndpoint::NoteDrop(std::size_t n) {
  const std::uint64_t prev = dropped_.fetch_add(n, std::memory_order_relaxed);
  if (prev == 0 || (prev + n) / 1000 != prev / 1000) {
    std::cerr << "McapWriterEndpoint: dropped " << (prev + n)
              << " frames (storage can't keep up with the recording)\n";
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
    if (resolved == nullptr) return;  // drop-until-mapped
    ch = std::make_shared<const Channel>(*resolved);
    channel_cache_.emplace(msg.stream_id, ch);
  }

  const std::size_t len = msg.payload.size();
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
  }
  cv_.notify_one();
}

void McapWriterEndpoint::WriterLoop() {
  for (;;) {
    std::deque<Entry> batch;
    {
      std::unique_lock<std::mutex> lk(mu_);
      cv_.wait(lk, [this] { return stop_ || !queue_.empty(); });
      batch.swap(queue_);
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
      batch.clear();
      continue;
    }
    try {
      DrainBatch(batch);
    } catch (const std::exception& e) {
      NoteFailure(e.what());
      batch.clear();
    } catch (...) {   // nothing may escape this frame; see above
      NoteFailure("unknown exception");
      batch.clear();
    }
  }
}

// nothrow: also reached from Stop(), which a noexcept destructor calls. The
// reason is logged here and nowhere else — deliberately not stored, so no
// allocation sits on the teardown path (a card dying and the heap failing are
// the same bad day) and callers need only the latch.
void McapWriterEndpoint::NoteFailure(const char* what) noexcept {
  if (!failed_.exchange(true, std::memory_order_relaxed)) {
    std::cerr << "McapWriterEndpoint: recording stopped — storage write failed: "
              << what << "\n";
  }
}

void McapWriterEndpoint::DrainBatch(std::deque<Entry>& batch) {
  for (auto& e : batch) {
    if (!e.channel) continue;
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
  batch.clear();
}

std::size_t McapWriterEndpoint::pending_frames() const {
  std::lock_guard<std::mutex> lk(mu_);
  return queue_.size();
}

std::size_t McapWriterEndpoint::pending_bytes() const {
  std::lock_guard<std::mutex> lk(mu_);
  return queue_bytes_;
}

std::uint64_t McapWriterEndpoint::bytes_written() const {
  // writer_ is created in the ctor and only Close()d (never reset) in Stop(),
  // so it stays valid for the endpoint's lifetime; bytes_written() reads an
  // atomic, so polling it from another thread needs no lock.
  return writer_ ? writer_->bytes_written() : 0;
}

McapWriterStats McapWriterEndpoint::stats() const {
  McapWriterStats s;
  s.writes = stat_writes_.load(std::memory_order_relaxed);
  s.blocked_ns = stat_blocked_ns_.load(std::memory_order_relaxed);
  s.max_block_ns = stat_max_block_ns_.load(std::memory_order_relaxed);
  s.slow_writes = stat_slow_writes_.load(std::memory_order_relaxed);
  return s;
}

}  // namespace visio_schema::mcap
