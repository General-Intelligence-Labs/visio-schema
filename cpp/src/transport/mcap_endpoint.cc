#include "visio_schema/transport/mcap_endpoint.hpp"

#include <iostream>
#include <utility>

namespace visio_schema::transport {

McapEndpoint::McapEndpoint(std::string_view path, StreamResolver resolve,
                           std::uint64_t max_bytes, double max_duration_s,
                           WritePolicy policy)
    : resolve_(std::move(resolve)),
      writer_(std::make_unique<visio_schema::mcap::McapWriter>(
          path, max_bytes, max_duration_s)),
      policy_(policy) {}

McapEndpoint::~McapEndpoint() { Close(); }

void McapEndpoint::Write(const Message& msg) {
  if (closed_) return;
  // Bound the in-RAM queue per policy (a stalled card must not grow memory).
  // Frames are flushed to disk later, on OnTick()/Close(). Age-based eviction is
  // intentionally not applied here — for a recording, dropping by oldest/byte-cap
  // is the right shedding, and entries carry no timestamp.
  const std::size_t len = msg.payload.size();
  const std::size_t before = queue_.size();
  if (!ApplyDropBound(policy_, queue_, queue_bytes_, len,
                      [](const Message& m) { return m.payload.size(); })) {
    NoteDrop(1);  // DropOnFail: queue full, this frame rejected
    return;
  }
  if (const std::size_t evicted = before - queue_.size()) NoteDrop(evicted);
  queue_.push_back(msg);
  queue_bytes_ += len;
}

void McapEndpoint::NoteDrop(std::size_t n) {
  const std::uint64_t prev = dropped_frames_;
  dropped_frames_ += n;
  // Surface recorder shedding instead of silently gapping the file; throttled to
  // the first drop and every 1000 thereafter.
  if (prev == 0 || dropped_frames_ / 1000 != prev / 1000) {
    std::cerr << "McapEndpoint: dropped " << dropped_frames_
              << " frames (storage can't keep up with the recording)\n";
  }
}

void McapEndpoint::Drain() {
  while (!queue_.empty()) {
    const Message& m = queue_.front();
    // Resolve at flush time: a DeviceInfo announce may have arrived after the
    // frame was queued (drop-until-mapped). Unmapped frames are dropped.
    const Channel* ch = resolve_ ? resolve_(m.stream_id) : nullptr;
    if (ch != nullptr) writer_->Write(*ch, m);
    queue_bytes_ -= m.payload.size();
    queue_.pop_front();
  }
}

void McapEndpoint::OnTick(std::int64_t /*now_ns*/) {
  if (!closed_) Drain();
}

void McapEndpoint::Close() {
  if (closed_) return;
  Drain();  // flush whatever is still queued before finalizing
  closed_ = true;
  if (writer_) writer_->Close();
}

}  // namespace visio_schema::transport
