// continuation — McapWriter's loss-preserving continuation parts: a cut
// staged until every local camera's keyframe is in, and forced at the
// ceiling.
#include "visio_schema/mcap/writer.hpp"

#include <algorithm>
#include <chrono>
#include <string>
#include <utility>

#include "mcap/writer/vendored_mcap.hpp"

namespace visio_schema::mcap {

bool McapWriter::ForcedCutDue() const {
  if (MustCut()) return true;
  if (max_bytes_ > 0) return part_bytes_ >= max_bytes_ + max_bytes_ / 8;
  return max_duration_ns_ > 0 && part_bytes_ > 0 &&
         std::chrono::steady_clock::now() - part_start_ >=
             std::chrono::nanoseconds(max_duration_ns_) + rotation_policy_->max_wait;
}

std::optional<std::chrono::steady_clock::time_point> McapWriter::rotation_deadline() const {
  if (staged_.empty()) return std::nullopt;
  return candidate_deadline_;
}

void McapWriter::FlushDue() {
  if (!staged_.empty() && std::chrono::steady_clock::now() >= candidate_deadline_)
    FlushStaged(false);
}

McapRotationStats McapWriter::rotation_stats() const {
  return {aligned_cuts_.load(), forced_cuts_.load(), cancelled_cuts_.load(),
          startup_dropped_.load(), stage_peak_.load()};
}

void McapWriter::WriteRotationMetadata() {
  if (!rotation_policy_) return;
  ::mcap::Metadata md;
  md.name = "visio.rotation";
  md.metadata = {{"version", "1"}, {"recording_id", recording_id_},
                 {"part_index", std::to_string(part_index_)},
                 {"video_continuation", "true"}, {"cut_reason", cut_reason_}};
  const auto status = writer_->write(md);
  if (!status.ok()) throw std::runtime_error("rotation metadata: " + status.message);
}

void McapWriter::ForceRollIfDue() {
  if (!ForcedCutDue()) return;
  cut_reason_ = "forced";
  Roll();
  forced_cuts_.fetch_add(1);
}

void McapWriter::FlushStaged(bool rotate, bool allow_forced) {
  if (staged_.empty()) return;
  if (rotate) {
    cut_reason_ = "aligned";
    Roll();
    aligned_cuts_.fetch_add(1);
  } else {
    cancelled_cuts_.fetch_add(1);
  }
  while (!staged_.empty()) {
    const auto& entry = staged_.front();
    if (allow_forced) ForceRollIfDue();
    const auto& message = entry.msg;
    WriteRecord(entry.channel, RecordedMessage{message.timestamp, message.seq,
                                               message.keyframe, message.payload.str()});
    staged_bytes_.fetch_sub(entry.bytes);
    staged_payload_bytes_ -= entry.msg.payload.size();
    staged_.pop_front();
  }
  candidate_ids_.clear();
}

void McapWriter::WriteContinuation(const Channel& channel, const RecordedMessage& msg) {
  FlushDue();
  const bool local = LocalVideo(channel);
  const auto stamp = TimestampNs(msg.timestamp);
  if (!staged_.empty() && local && stamp < candidate_min_ns_ - rotation_policy_->tolerance_ns) {
    // Preserve this camera's order without moving the candidate IDRs backwards.
    // If the old part has reached its ceiling, cancel the candidate first.
    if (ForcedCutDue()) {
      FlushStaged(false);
      ForceRollIfDue();
    }
    WriteRecord(channel, msg); // a lagging local camera's preceding GOP
    return;
  }
  if (!staged_.empty() && local && candidate_ids_.count(channel.id) == 0) {
    const auto spread = std::max(stamp, candidate_max_ns_) - std::min(stamp, candidate_min_ns_);
    if (!msg.keyframe || spread > rotation_policy_->tolerance_ns) FlushStaged(false);
  }
  if (staged_.empty() && rotating_ && ShouldRoll()) {
    if (ForcedCutDue() || rotation_policy_->local_video_ids.empty() || !rotate_on_keyframe_) {
      cut_reason_ = "forced";
      Roll();
      forced_cuts_.fetch_add(1);
    } else if (local && msg.keyframe) {
      candidate_min_ns_ = candidate_max_ns_ = stamp;
      candidate_deadline_ = std::chrono::steady_clock::now() + rotation_policy_->max_wait;
      if (max_bytes_ == 0)
        candidate_deadline_ = std::min(candidate_deadline_, part_start_ +
            std::chrono::nanoseconds(max_duration_ns_) + rotation_policy_->max_wait);
      candidate_ids_.clear();
      candidate_ids_.insert(channel.id);
    }
  }
  const bool candidate = !staged_.empty() || !candidate_ids_.empty();
  if (!candidate) { WriteRecord(channel, msg); return; }
  const std::size_t bytes = sizeof(Pending) + msg.payload.size() + channel.schema.size() +
                            channel.topic.size() + channel.schema_name.size();
  if (bytes > rotation_policy_->max_staged_bytes ||
      staged_bytes_.load() > rotation_policy_->max_staged_bytes - bytes) {
    FlushStaged(false);
    candidate_ids_.clear();
    ForceRollIfDue();
    WriteRecord(channel, msg);
    return;
  }
  // RecordedMessage borrows bytes only across Write(). Rotation may wait for
  // another camera after this call, when the endpoint has recycled its arena.
  Message retained;
  retained.timestamp = msg.timestamp;
  retained.seq = msg.seq;
  retained.keyframe = msg.keyframe;
  retained.payload = wire::Payload(msg.payload);
  staged_.push_back({channel, std::move(retained), bytes});
  staged_payload_bytes_ += msg.payload.size();
  const auto resident = staged_bytes_.fetch_add(bytes) + bytes;
  stage_peak_.store(std::max(stage_peak_.load(), resident));
  if (local && msg.keyframe && candidate_ids_.insert(channel.id).second) {
    candidate_min_ns_ = std::min(candidate_min_ns_, stamp);
    candidate_max_ns_ = std::max(candidate_max_ns_, stamp);
  }
  if (candidate_ids_.size() == rotation_policy_->local_video_ids.size()) FlushStaged(true);
  else if (max_bytes_ && part_bytes_ + staged_payload_bytes_ >= max_bytes_ + max_bytes_ / 8)
    FlushStaged(false);
}

}  // namespace visio_schema::mcap
