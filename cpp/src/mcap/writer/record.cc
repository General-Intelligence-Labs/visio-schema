// record — McapWriter's records: each written into the current part (a
// video stream warmed up on its keyframe), the capture metadata, closing
// the recording, and what the writer reports.
#include "visio_schema/mcap/writer.hpp"

#include <algorithm>
#include <cstdint>
#include <map>
#include <string>
#include <utility>

#include "visio_schema/log.hpp"

#include "mcap/part_outcome.hpp"
#include "mcap/span_readback.hpp"
#include "mcap/writer/vendored_mcap.hpp"

namespace visio_schema::mcap {

namespace {

// The protobuf full name Foxglove uses for H.265 video (a camera channel's
// Channel::schema_name). Only these channels are keyframe-gated; audio
// ("foxglove.RawAudio"), IMU, encoder and control are written unconditionally.
constexpr const char* kCompressedVideoSchema = "foxglove.CompressedVideo";
// ~5 s at 60 fps, 10 s at 30 — well past any real GOP wait, and early enough
// to fire inside a short session.
constexpr std::uint64_t kUnprimedVideoWarnFrames = 300;

}  // namespace

void McapWriter::SetMetadata(std::string name,
                             std::map<std::string, std::string> kv) {
  meta_name_ = std::move(name);
  meta_ = std::move(kv);
  WriteStoredMetadata();  // into the current (first) part now
}

void McapWriter::WriteStoredMetadata() {
  if (!writer_ || meta_.empty()) return;
  ::mcap::Metadata md;
  md.name = meta_name_;
  for (const auto& [k, v] : meta_) md.metadata[k] = v;
  // Best-effort; a metadata-write failure never aborts a recording — but it
  // must not be silent either (the part would ship without capture meta).
  const ::mcap::Status st = writer_->write(md);
  if (!st.ok())
    log::Write(log::Severity::kError,
               "McapWriter: metadata record write failed: %s",
               st.message.c_str());
}

void McapWriter::Write(const Channel& channel, const Message& msg) {
  Write(channel, RecordedMessage{msg.timestamp, msg.seq, msg.keyframe,
                                 msg.payload.str(), msg.sync_point});
}

// Nothing is written once the recording has ended (storage_refused(), which
// the endpoint sheds and counts on).
void McapWriter::Write(const Channel& channel, const RecordedMessage& msg) {
  if (closed_ || !writer_ || storage_refused()) return;
  if (rotation_policy_) WriteContinuation(channel, msg);
  else WriteRecord(channel, msg);
}

void McapWriter::WriteRecord(const Channel& channel, const RecordedMessage& msg) {
  if (closed_ || !writer_ || storage_refused()) return;

  const bool is_video = channel.schema_name == kCompressedVideoSchema;
  const std::int64_t msg_ts_ns = TimestampNs(msg.timestamp);

  if (!rotation_policy_ && rotate_on_keyframe_ && rotating_) {
    // Cut ONLY at a video keyframe that begins a pair NOT yet started in this
    // part — one strictly newer than every video frame already written, plus a
    // guard so a pair's µs-skewed sibling keyframe (which may already be in this
    // part) can never trigger the cut and split the pair. If the cap is crossed
    // mid-GOP, the current GOP finishes into the old part and the cut lands at the
    // next boundary (≤1 extra GOP overshoot). A byte hard ceiling (+1/8) still
    // rolls if video keyframes ever stall, so a stuck encoder can't grow a part
    // unbounded — this only bounds BYTE rotation (max_bytes>0), the sole on-device
    // mode; a duration-only rotate_on_keyframe would have no stall ceiling.
    const bool starts_new_pair =
        is_video && msg.keyframe && msg_ts_ns > part_max_video_ts_ + pair_guard_ns_;
    if (ShouldRoll() && starts_new_pair) {
      Roll();
    } else if (MustCut() ||
               (max_bytes_ > 0 && part_bytes_ >= max_bytes_ + max_bytes_ / 8)) {
      Roll();
    }
  } else if (!rotation_policy_ && rotating_ && ShouldRoll()) {
    Roll();  // opt-in off: original byte-exact roll
  }
  if (!writer_ || part_->refused()) return;  // the roll ended the recording

  // Initial decoder warm-up: legacy callers repeat it per part; a continuation
  // chain repeats it only for each local camera's first-ever IDR. Continuation
  // P-frames reference the previous part and must be retained verbatim.
  if (is_video && (!rotation_policy_ || SelectedVideo(channel)) &&
      primed_video_channels_.find(channel.id) == primed_video_channels_.end()) {
    if (!msg.keyframe) {
      startup_dropped_.fetch_add(1, std::memory_order_relaxed);
      // Normally a handful of frames until the next IDR (GOP is one second).
      // A channel that NEVER primes is a different animal: its whole topic is
      // absent from the recording and nothing else reports it. That is what a
      // relayed leaf looked like before Header.keyframe was serialized, and it
      // is what a rig running mixed firmware still looks like — a leaf too old
      // to set the wire flag can never prime. Warn once per channel, well past
      // any legitimate wait.
      auto& n = unprimed_video_frames_[channel.topic];
      if (++n == kUnprimedVideoWarnFrames) {
        log::Write(
            log::Severity::kWarning,
            "McapWriter: %s has sent %llu video frames with no keyframe — "
            "the whole topic is being dropped from this recording. If it is "
            "relayed, its device may predate the wire keyframe flag.",
            channel.topic.c_str(), static_cast<unsigned long long>(n));
      }
      // Counted only for a stream this recording already carried: a rotation
      // that opened mid-GOP leaves a hole. The frames before a stream's very
      // first keyframe precede the recording's start for that stream and are
      // no gap in it.
      if (ever_primed_video_channels_.count(channel.id) != 0)
        keyframe_gated_.fetch_add(1, std::memory_order_relaxed);
      return;  // pre-keyframe P-frame — dropped
    }
    primed_video_channels_.insert(channel.id);
    ever_primed_video_channels_.insert(channel.id);
    unprimed_video_frames_.erase(channel.topic);
  }

  auto sit = schema_ids_.find(channel.schema_name);
  if (sit == schema_ids_.end()) {
    // Schema.name is the protobuf full name; Schema.data is the
    // FileDescriptorSet, so Foxglove resolves the type inside it.
    ::mcap::Schema schema(channel.schema_name,
                        channel.schema_encoding.empty() ? kDefaultEncoding
                                                        : channel.schema_encoding,
                        channel.schema);
    writer_->addSchema(schema);
    sit = schema_ids_.emplace(channel.schema_name, schema.id).first;
  }

  auto cit = channel_ids_.find(channel.id);
  if (cit == channel_ids_.end()) {
    ::mcap::Channel ch(
        channel.topic,
        channel.encoding.empty() ? kDefaultEncoding : channel.encoding,
        sit->second);
    writer_->addChannel(ch);
    cit = channel_ids_.emplace(channel.id, ch.id).first;
  }

  const auto ts = static_cast<::mcap::Timestamp>(msg_ts_ns);
  ::mcap::Message out;
  out.channelId = cit->second;
  out.sequence = msg.seq;
  out.logTime = ts;
  out.publishTime = ts;
  out.dataSize = msg.payload.size();
  out.data = reinterpret_cast<const std::byte*>(msg.payload.data());
  // mcap refuses a message only on its own bookkeeping (not open, unknown
  // channel or schema) — disk errors surface through the file, not here. So a
  // refusal is a bug in this writer: report it, never abort the recording,
  // and count none of its bytes, or an all-refused part escapes the empty-part
  // drop in CloseCurrentPart and drives ShouldRoll with bytes never written.
  const ::mcap::Status st = writer_->write(out);
  if (!st.ok()) {
    if (!part_write_failure_logged_) {
      part_write_failure_logged_ = true;
      log::Write(log::Severity::kError,
                 "McapWriter: message refused (later refusals in this part "
                 "not logged): %s",
                 st.message.c_str());
    }
    return;
  }
  // A failed write costs its part, whose bytes still count toward the roll
  // that ends it; one storage refused for good is not counted at all.
  if (part_->refused()) return;
  part_bytes_ += msg.payload.size();
  // Track the newest video capture time WRITTEN this part — the rotate-on-keyframe
  // "starts a new pair" test compares against it (P-frames included, so the next
  // GOP's keyframe beats the last P-frame while a pair's sibling does not).
  if (is_video && msg_ts_ns > part_max_video_ts_) part_max_video_ts_ = msg_ts_ns;
  // Lifetime total — monotonic across part rotation (OpenPart resets part_bytes_
  // but never this), so a poller can distinguish active writing from a stall.
  bytes_written_.fetch_add(msg.payload.size(), std::memory_order_relaxed);
}

void McapWriter::Close() {
  if (closed_) return;
  if (start_status_.load() == StartStatus::Ready) {
    FlushStaged(false, false);
  } else {
    staged_.clear();
    staged_bytes_.store(0);
    staged_payload_bytes_ = 0;
    candidate_ids_.clear();
  }
  closed_ = true;
  if (writer_) CloseCurrentPart();
  // A part that lost bytes cannot have written more than its files hold.
  if (part_->refused() || part_->unconfirmed_parts() > 0)
    bytes_written_.store(std::min(bytes_written_.load(), physical_closed_));
  // Bounded by close_flush_ms (0 = none); whatever is left is counted
  // skipped and the ring is released.
  if (readback_) readback_->Finish();
}

bool McapWriter::SelectedVideo(const Channel& channel) const {
  if (!rotation_policy_ || channel.schema_name != kCompressedVideoSchema) return false;
  const auto& ids = rotation_policy_->video_ids;
  return std::find(ids.begin(), ids.end(), channel.id) != ids.end();
}

bool McapWriter::ReadbackStep(std::chrono::milliseconds budget) {
  return readback_ && readback_->Step(budget);
}

std::size_t McapWriter::readback_pending() const {
  return readback_ ? readback_->pending() : 0;
}

McapReadbackStats McapWriter::readback_stats() const {
  return readback_ ? readback_->stats() : McapReadbackStats{};
}

bool McapWriter::storage_fault() const {
  return storage_refused() || unconfirmed_parts() > 0 ||
         drop_failures_.load(std::memory_order_relaxed) > 0 ||
         (readback_ && readback_->storage_fault());
}

bool McapWriter::storage_refused() const {
  return part_->refused() || ended_.load(std::memory_order_relaxed);
}

std::uint64_t McapWriter::part_open_retries() const {
  return part_open_retries_.load(std::memory_order_relaxed);
}

std::uint64_t McapWriter::unconfirmed_parts() const {
  return part_->unconfirmed_parts();
}

}  // namespace visio_schema::mcap
