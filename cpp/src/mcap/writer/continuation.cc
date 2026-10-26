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
             std::chrono::nanoseconds(max_duration_ns_) +
                 rotation_policy_->idr_wait + rotation_policy_->max_wait;
}

std::optional<std::chrono::steady_clock::time_point>
McapWriter::rotation_deadline() const {
  if (!staged_.empty())
    return candidate_deadline_;
  if (start_status_.load() == StartStatus::Waiting)
    return start_deadline_;
  return std::nullopt;
}

void McapWriter::FlushDue() {
  const auto deadline = rotation_deadline();
  if (!deadline || std::chrono::steady_clock::now() < *deadline)
    return;
  if (start_status_.load() == StartStatus::Waiting) {
    FailStart();
    return;
  }
  FlushStaged(false);
}

void McapWriter::FailStart() {
  start_status_.store(StartStatus::TimedOut);
  staged_.clear();
  candidate_ids_.clear();
  staged_bytes_.store(0);
  staged_payload_bytes_ = 0;
}

void McapWriter::ArmStart(wire::SyncPoint target, std::chrono::milliseconds wait) {
  if (wait.count()<0) throw std::invalid_argument("negative recording start budget");
  if (start_status_.load() != StartStatus::Waiting)
    return;
  if (!candidate_ids_.empty())
    throw std::logic_error("recording start already receiving frames");
  start_target_ = target;
  start_deadline_ =
      std::chrono::steady_clock::now() + (wait.count()?wait:rotation_policy_->start_wait);
}

McapRotationStats McapWriter::rotation_stats() const {
  return {aligned_cuts_.load(), forced_cuts_.load(), cancelled_cuts_.load(),
          startup_dropped_.load(), stage_peak_.load()};
}

void McapWriter::WriteRotationMetadata() {
  if (!rotation_policy_)
    return;
  ::mcap::Metadata md;
  md.name = "visio.rotation";
  md.metadata = {
      {"version", "1"},
      {"recording_id", recording_id_},
      {"part_index", std::to_string(part_index_)},
      {"video_continuation", cut_reason_ == "forced" ? "true" : "false"},
      {"cut_reason", cut_reason_},
      {"authority_epoch", std::to_string(candidate_point_.authority_epoch)},
      {"grid_generation", std::to_string(candidate_point_.grid_generation)},
      {"frame_index", std::to_string(candidate_point_.frame_index)}};
  const auto status = writer_->write(md);
  if (!status.ok())
    throw std::runtime_error("rotation metadata: " + status.message);
}

void McapWriter::ForceRollIfDue() {
  if (!ForcedCutDue())
    return;
  cut_reason_ = "forced";
  Roll();
  forced_cuts_.fetch_add(1);
}

void McapWriter::FlushStaged(bool rotate, bool allow_forced) {
  if (staged_.empty())
    return;
  const bool starting = start_status_.load() == StartStatus::Waiting;
  if (rotate && !starting) {
    cut_reason_ = "aligned";
    Roll();
    aligned_cuts_.fetch_add(1);
  } else if (!rotate)
    cancelled_cuts_.fetch_add(1);
  if (starting && rotate) {
    ::mcap::Metadata metadata;
    metadata.name = "visio.sync.start";
    std::string streams;
    for (const auto id : rotation_policy_->video_ids) {
      if (!streams.empty())
        streams += ",";
      streams += std::to_string(id);
    }
    metadata.metadata = {
        {"authority_epoch", std::to_string(candidate_point_.authority_epoch)},
        {"grid_generation", std::to_string(candidate_point_.grid_generation)},
        {"frame_index", std::to_string(candidate_point_.frame_index)},
        {"video_stream_ids", streams}};
    const auto status = writer_->write(metadata);
    if (!status.ok())
      throw std::runtime_error("sync metadata: " + status.message);
  }
  while (!staged_.empty()) {
    const auto &entry = staged_.front();
    if (allow_forced && (!rotate || MustCut()))
      ForceRollIfDue();
    const auto &message = entry.msg;
    WriteRecord(entry.channel,
                RecordedMessage{message.timestamp, message.seq,
                                message.keyframe, message.payload.str(),
                                message.sync_point});
    staged_bytes_.fetch_sub(entry.bytes);
    staged_payload_bytes_ -= message.payload.size();
    staged_.pop_front();
  }
  candidate_ids_.clear();
  if (starting && rotate)
    start_status_.store(StartStatus::Ready);
}

void McapWriter::WriteContinuation(const Channel &channel,
                                   const RecordedMessage &msg) {
  FlushDue();
  if (start_status_.load() == StartStatus::TimedOut)
    return;
  const bool video = channel.schema_name == kCompressedVideoSchema;
  const bool selected = SelectedVideo(channel);
  // Freeze the video membership for this take. Newly discovered cameras join
  // the next recording; they cannot silently change an in-progress barrier.
  if (video && !selected)
    return;
  const auto stamp = TimestampNs(msg.timestamp);
  const bool starting = start_status_.load() == StartStatus::Waiting;
  if (starting && start_target_.valid() && selected &&
      !candidate_ids_.count(channel.id) &&
      (!msg.keyframe || msg.sync_point != start_target_)) {
    startup_dropped_.fetch_add(1);
    return;
  }
  const bool idr = selected && msg.keyframe && msg.sync_point.valid() &&
                   (!candidate_ids_.empty() || starting ||
                    msg.sync_point != candidate_point_);
  if (!candidate_ids_.empty() && selected &&
      !candidate_ids_.count(channel.id)) {
    if (stamp < candidate_min_ns_ - rotation_policy_->tolerance_ns) {
      if (starting)
        startup_dropped_.fetch_add(1);
      else {
        if (ForcedCutDue()) {
          FlushStaged(false);
          ForceRollIfDue();
        }
        WriteRecord(channel, msg); // this camera's preceding frames stay old
      }
      return;
    }
    const auto spread =
        std::max(stamp, candidate_max_ns_) - std::min(stamp, candidate_min_ns_);
    if (!idr || msg.sync_point != candidate_point_ ||
        spread > rotation_policy_->tolerance_ns) {
      if (starting) {
        FailStart();
        return;
      }
      FlushStaged(false);
    }
  }
  if (candidate_ids_.empty()) {
    const bool due = starting || (rotating_ && ShouldRoll());
    if (!starting && due &&
        (ForcedCutDue() || rotation_policy_->video_ids.empty() ||
         !rotate_on_keyframe_)) {
      cut_reason_ = "forced";
      Roll();
      forced_cuts_.fetch_add(1);
    } else if (due && idr) {
      candidate_point_ = msg.sync_point;
      candidate_min_ns_ = candidate_max_ns_ = stamp;
      candidate_deadline_ =
          std::chrono::steady_clock::now() + rotation_policy_->max_wait;
      if (starting)
        // Wake/encoder/network arrival skew is not capture-time skew. The
        // first complete IDR group uses the bounded recording-start budget;
        // max_wait remains the independent steady-state rotation window.
        candidate_deadline_ = start_deadline_;
      else if (max_bytes_ == 0 && max_duration_ns_ > 0)
        candidate_deadline_ = std::min(
            candidate_deadline_,
            part_start_ + std::chrono::nanoseconds(max_duration_ns_) +
                rotation_policy_->idr_wait + rotation_policy_->max_wait);
      candidate_ids_.insert(channel.id);
    }
  }
  if (candidate_ids_.empty()) {
    if (starting) {
      if (video)
        startup_dropped_.fetch_add(1);
      return;
    }
    WriteRecord(channel, msg);
    return;
  }
  const std::size_t bytes = sizeof(Pending) + msg.payload.size() +
                            channel.schema.size() + channel.topic.size() +
                            channel.schema_name.size();
  if (bytes > rotation_policy_->max_staged_bytes ||
      staged_bytes_.load() > rotation_policy_->max_staged_bytes - bytes) {
    if (starting) {
      FailStart();
      return;
    }
    FlushStaged(false);
    ForceRollIfDue();
    WriteRecord(channel, msg);
    return;
  }
  Message retained;
  retained.timestamp = msg.timestamp;
  retained.seq = msg.seq;
  retained.keyframe = msg.keyframe;
  retained.sync_point = msg.sync_point;
  retained.payload = wire::Payload(msg.payload);
  staged_.push_back({channel, std::move(retained), bytes});
  staged_payload_bytes_ += msg.payload.size();
  const auto resident = staged_bytes_.fetch_add(bytes) + bytes;
  stage_peak_.store(std::max(stage_peak_.load(), resident));
  if (idr && msg.sync_point == candidate_point_ &&
      candidate_ids_.insert(channel.id).second) {
    candidate_min_ns_ = std::min(candidate_min_ns_, stamp);
    candidate_max_ns_ = std::max(candidate_max_ns_, stamp);
  }
  if (candidate_ids_.size() == rotation_policy_->video_ids.size())
    FlushStaged(true);
  else if (!starting && max_bytes_ &&
           part_bytes_ + staged_payload_bytes_ >= max_bytes_ + max_bytes_ / 8)
    FlushStaged(false);
}

}  // namespace visio_schema::mcap
