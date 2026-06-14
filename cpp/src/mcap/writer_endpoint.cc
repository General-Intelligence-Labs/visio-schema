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
                                       transport::WritePolicy policy)
    : resolve_(std::move(resolve)),
      writer_(std::make_unique<visio_schema::mcap::McapWriter>(
          path, max_bytes, max_duration_s)),
      policy_(policy) {}

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
  if (writer_) writer_->Close();
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
    DrainBatch(batch);
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

McapWriterStats McapWriterEndpoint::stats() const {
  McapWriterStats s;
  s.writes = stat_writes_.load(std::memory_order_relaxed);
  s.blocked_ns = stat_blocked_ns_.load(std::memory_order_relaxed);
  s.max_block_ns = stat_max_block_ns_.load(std::memory_order_relaxed);
  s.slow_writes = stat_slow_writes_.load(std::memory_order_relaxed);
  return s;
}

}  // namespace visio_schema::mcap
