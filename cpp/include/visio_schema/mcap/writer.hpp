// McapWriter — write (Channel, Message) pairs to an MCAP file (the Foxglove
// container format). Mirrors python/visio_schema/mcap/writer.py:McapWriter.
//
// The canonical Visio MCAP writer, with no bus or resolver: the caller hands in
// the resolved Channel (topic + schema) and the Message. Payload bytes are
// stored verbatim (already-serialized protobuf). Schema/channel registration is
// lazy: one schema per schema_name, one channel per Channel.id.
//
// Schema naming: a protobuf channel's Schema.name is the payload's protobuf full
// name (Channel::schema_name) and Schema.data is its FileDescriptorSet
// (Channel::schema), so Foxglove resolves the type from the embedded set.
//
// Rotation: pass max_bytes and/or max_duration_s to split into numbered
// numbered parts <stem>_0000<ext>, <stem>_0001<ext>, … max_bytes counts written
// payload bytes (approximate).
//
// Embeddable: the vendored header-only mcap writer is pulled into the .cc alone
// (lz4/zstd compiled out, Compression::None), and this header does NOT expose
// any mcap type, so consumers (and the visio McapEndpoint adapter) compile with
// no MCAP include and the whole sink cross-compiles for the device.
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <map>
#include <deque>
#include <vector>
#include <memory>
#include <string>
#include <optional>
#include <string_view>

#include "visio_schema/mcap/storage.hpp"
#include "visio_schema/mcap/readback.hpp"
#include "visio_schema/mcap/recording_crypto.hpp"
#include <unordered_map>
#include <unordered_set>

#include "visio_schema/routing/channel.hpp"   // Channel
#include "visio_schema/wire/message.hpp"
#include "visio_schema/wire/time.hpp"          // TimestampNs

namespace mcap {
class McapWriter;
class IWritable;
}

namespace visio_schema::mcap {

using visio_schema::wire::Message;

// What the writer records of a message: its timing, sequence, sync flag and
// payload bytes. A view, so the payload can live in any buffer the caller
// keeps alive across Write().
struct RecordedMessage {
  google_protobuf_Timestamp timestamp = google_protobuf_Timestamp_init_zero;
  std::uint32_t seq = 0;
  bool keyframe = false;
  std::string_view payload;
  wire::SyncPoint sync_point;
};

class SpanReadback;

// Firmware freezes all selected video streams, including relayed cameras.
// All continuation records survive even when a cut cannot be aligned.
struct McapRotationPolicy {
  std::vector<std::uint32_t> video_ids;
  std::int64_t tolerance_ns = 1'000'000;
  std::size_t max_staged_bytes = 8 * 1024 * 1024;
  std::chrono::milliseconds max_wait{100};
  std::chrono::milliseconds start_wait{1100};
  std::chrono::milliseconds idr_wait{1000};
};

struct McapRotationStats {
  std::uint64_t aligned = 0, forced = 0, cancelled = 0;
  std::uint64_t startup_dropped = 0, max_staged_bytes = 0;
};

class McapWriter {
 public:
  // Record to a filesystem path. With max_bytes > 0 and/or max_duration_s > 0,
  // rotate into numbered parts (path becomes <stem>_NNN<ext>); 0 disables that
  // axis. Throws std::runtime_error if a part can't be opened.
  //
  // rotate_on_keyframe (opt-in): when rotating by size, defer the cut to the next
  // compressed-video keyframe that begins a pair not yet started in the current
  // part, so every part opens each video stream on a decodable IDR and no
  // co-phased stereo pair is split. REQUIRES that the producer emit co-phased
  // IDRs on multi-cam recordings (else the non-triggering eye loses up to a GOP
  // per rotation). pair_guard_ns (≈ half a frame period) separates a pair's
  // µs-skewed sibling keyframe from the next GOP's keyframe. Both default off, so
  // a caller that does not opt in keeps the plain byte-exact roll.
  //
  // rotation_policy (opt-in): use loss-preserving continuation parts. Only the
  // selected video ids must confirm one shared SyncPoint. Bound staging
  // by max_staged_bytes/max_wait; cancel an incomplete candidate into the old
  // part. At the byte ceiling (+1/8), force a cut and retain P-frames. Readers
  // must decode the marked recording chain across forced cuts. Initial video
  // waits for a complete confirmed group under a bounded start deadline.
  // pair_guard_ns above belongs to the legacy policy, not this timestamp-spread
  // comparison. Descriptors and payload timestamps/sequences remain unchanged.
  //
  // sync_span_bytes (opt-in, 0 = off): hand the part file's bytes to kernel
  // writeback in spans of at least this many bytes as they are written
  // (page-aligned; NOT an MCAP Chunk boundary), keeping the dirty page set
  // bounded instead of letting the kernel accumulate and stall the writer.
  // Linux-only; elsewhere it degrades to a periodic fflush. Output bytes
  // are identical either way. Full rationale at SyncSpan() in writer.cc.
  //
  // recording_key (opt-in): write each part as a `VREC` container — a 32-byte
  // plaintext header, then the MCAP stream under ChaCha20 — instead of
  // plaintext MCAP. Filename and rotation are unchanged; only the bytes
  // differ, so every reader must sniff the magic. Absent means plaintext, and
  // that is a RUNTIME choice: whether a recording is encrypted depends on
  // whether the device holds a key, never on how this was compiled.
  //
  // readback (opt-in): verify each writeback span against the medium while
  // its bytes are still in RAM; see readback.hpp.
  explicit McapWriter(std::string_view path, std::uint64_t max_bytes = 0,
                      double max_duration_s = 0.0, bool rotate_on_keyframe = false,
                      std::int64_t pair_guard_ns = 0,
                      std::uint64_t sync_span_bytes = 0,
                      std::optional<RecordingKey> recording_key = std::nullopt,
                      McapReadbackOptions readback = {},
                      std::shared_ptr<WriterStorage> storage = {},
                      std::chrono::milliseconds checkpoint_interval = {},
                      std::optional<McapRotationPolicy> rotation_policy = std::nullopt);
  ~McapWriter();

  McapWriter(const McapWriter&) = delete;
  McapWriter& operator=(const McapWriter&) = delete;

  // Record one message against `channel` (lazily registering its schema +
  // channel records — declare-before-write).
  void Write(const Channel& channel, const Message& msg);
  // The same, for a message whose payload is held somewhere other than a
  // Message (a queue's own buffer); `msg.payload` need only outlive the call.
  void Write(const Channel& channel, const RecordedMessage& msg);
  void Close();
  // Only the writer worker mutates staging. Polling gauges are atomic.
  void FlushDue();
  enum class StartStatus { Waiting, Ready, TimedOut };
  StartStatus start_status() const { return start_status_.load(); }
  // Preparation-time only, before the worker receives any frames.
  // Called before input/worker start. A nonzero budget starts AFTER file
  // creation; zero retains the rotation policy's configured start budget.
  void ArmStart(wire::SyncPoint target,
                std::chrono::milliseconds wait = std::chrono::milliseconds::zero());
  std::optional<std::chrono::steady_clock::time_point> rotation_deadline() const;
  std::uint64_t rotation_pending_bytes() const { return staged_bytes_.load(); }
  McapRotationStats rotation_stats() const;


  // Carry an MCAP Metadata record (`name` + key/values) in the file — e.g. the
  // capture metadata (task/location/…). Call once after construction; it's
  // written into the current part immediately and re-emitted on each rotation so
  // every part is self-describing. Not thread-safe vs Write; call before the
  // first Write / before a writer thread starts draining.
  void SetMetadata(std::string name, std::map<std::string, std::string> kv);

  // Total payload bytes written to disk over this writer's lifetime. Unlike
  // part_bytes_ (which resets at each rotation) this is monotonic across parts,
  // so a stall detector can watch it advance to tell "actively writing" from
  // "recording but the pipeline is frozen". Read-only, safe to poll from
  // another thread.
  std::uint64_t bytes_written() const {
    return bytes_written_.load(std::memory_order_relaxed);
  }

  // Video frames the keyframe gate dropped after a part rotation, before the
  // new part saw the stream's next keyframe: a hole in the recording. The
  // frames before a stream's first keyframe of the whole recording are not
  // counted — the recording starts there. Monotonic; safe to poll from
  // another thread.
  std::uint64_t keyframe_gated() const {
    return keyframe_gated_.load(std::memory_order_relaxed);
  }

  // Read-back pull API (see readback.hpp). ReadbackStep verifies at most
  // one span — resuming one a previous budget left unfinished — and
  // returns true if it did any work, false when nothing is ready or queued.
  // A step may overrun its budget by one piece. ONE stepping thread at a
  // time; it may run concurrently with Write and is serialized against
  // Close by an internal lock, so Close waits for a step in progress (the
  // budget plus one piece) before its own bounded flush. Never blocks the
  // writer. A no-op (false) when read-back is off.
  bool ReadbackStep(std::chrono::milliseconds budget);
  // Spans queued or in progress. Nonzero with a false step means the front
  // span is waiting for settle_bytes of further writes.
  std::size_t readback_pending() const;
  McapReadbackStats readback_stats() const;
  // Latched once a span stayed wrong after its rewrite (or the rewrite
  // itself failed). Recording continues; the owner decides what to do.
  bool storage_fault() const;

 private:
  struct Pending { Channel channel; Message msg; std::size_t bytes; };
  void WriteRecord(const Channel& channel, const RecordedMessage& msg);
  void WriteContinuation(const Channel& channel, const RecordedMessage& msg);
  void FlushStaged(bool rotate, bool allow_forced = true);
  void FailStart();
  void ForceRollIfDue();
  bool SelectedVideo(const Channel& channel) const;
  bool ForcedCutDue() const;
  void WriteRotationMetadata();
  std::string PartPath() const;
  void OpenPart();          // throws on open failure
  bool ShouldRoll() const;
  void Roll();
  // Close the current part through checked durability and namespace barriers.
  void CloseCurrentPart();
  // Emit the stored Metadata record into the current part (no-op if none set).
  void WriteStoredMetadata();

  std::string meta_name_;
  std::map<std::string, std::string> meta_;

  const std::optional<McapRotationPolicy> rotation_policy_;
  std::string recording_id_, cut_reason_ = "initial";
  std::deque<Pending> staged_;
  std::unordered_set<std::uint32_t> candidate_ids_;
  wire::SyncPoint candidate_point_,start_target_;
  std::atomic<StartStatus> start_status_{StartStatus::Ready};
  std::chrono::steady_clock::time_point start_deadline_;
  std::int64_t candidate_min_ns_ = 0, candidate_max_ns_ = 0;
  std::uint64_t staged_payload_bytes_ = 0;
  std::chrono::steady_clock::time_point candidate_deadline_;
  std::atomic<std::uint64_t> staged_bytes_{0}, aligned_cuts_{0}, forced_cuts_{0};
  std::atomic<std::uint64_t> cancelled_cuts_{0}, startup_dropped_{0}, stage_peak_{0};
  const std::string base_path_;
  const std::uint64_t max_bytes_;
  const std::int64_t max_duration_ns_;
  const bool rotating_;
  const bool rotate_on_keyframe_;
  const std::int64_t pair_guard_ns_;
  const std::uint64_t sync_span_bytes_;
  // Set once at construction: a recording cannot change key mid-file, because
  // each part's header names the key that opens it. A rotation picks it up.
  const std::optional<RecordingKey> recording_key_;

  std::atomic<bool> io_fault_{false};
  const std::shared_ptr<WriterStorage> storage_;
  const std::chrono::milliseconds checkpoint_interval_;
  IoResult last_io_;
  std::uint64_t physical_closed_ = 0;

  // Write-time read-back; null when off. Declared before file_: the
  // writable holds a raw pointer into it and reports its tail from end(),
  // so this must be destroyed after it.
  std::unique_ptr<SpanReadback> readback_;

  // The IWritable backing writer_'s current part. We own the underlying fd
  // (opened with O_CLOEXEC) rather than letting upstream mcap fopen() it, so a
  // recording fd is never inherited by a child we fork+exec (the long-lived
  // Wi-Fi AP daemons). A leaked recording fd would pin /mnt/sdcard busy and make
  // a later `umount` (and thus the "format SD card" command) fail with EBUSY.
  // Declared before writer_ so it is destroyed *after* it: ~McapWriter()/close()
  // flush through this writable on teardown.
  std::unique_ptr<::mcap::IWritable> file_;
  std::unique_ptr<::mcap::McapWriter> writer_;
  bool closed_ = false;
  std::size_t part_index_ = 0;
  std::uint64_t part_bytes_ = 0;
  // A message write that failed in the current part has been reported. Once
  // per part: this is the per-message path, and a failing sink fails them all.
  bool part_write_failure_logged_ = false;
  // Lifetime total (never reset on rotation); see bytes_written().
  std::atomic<std::uint64_t> bytes_written_{0};
  std::atomic<std::uint64_t> keyframe_gated_{0};
  std::chrono::steady_clock::time_point part_start_;

  // Caches (reset per part): schema id per schema_name, channel id per Channel id.
  std::unordered_map<std::string, std::uint16_t> schema_ids_;
  std::unordered_map<std::uint32_t, std::uint16_t> channel_ids_;

  // Per-part keyframe gate (reset in OpenPart): the set of compressed-video
  // Channel ids that have already seen their first keyframe of this part. A video
  // channel's frames are dropped until it appears here, so every part opens each
  // video stream on a decodable IDR. `part_max_video_ts_` is the newest capture
  // timestamp of any video frame *written* to the current part; rotate-on-keyframe
  // cuts only on a keyframe strictly newer than it (+guard), which is what keeps a
  // co-phased pair whole across a rotation boundary.
  std::unordered_set<std::uint32_t> primed_video_channels_;
  // Every video channel primed in ANY part of this writer: what tells a
  // rotation's gated frames (a hole) from a stream's start (not one).
  std::unordered_set<std::uint32_t> ever_primed_video_channels_;
  // Frames refused by the keyframe gate on a channel that has not primed yet,
  // keyed by TOPIC. Deliberately outside the per-part block above and NOT reset
  // by OpenPart: the question it answers ("is this topic absent from the whole
  // recording?") spans parts, and re-arming it per part would warn once every
  // rotation about one unchanging fault.
  //
  // Topic, not Channel::id, because ids do not survive a reconnect — the bus
  // allocates a fresh one per link and never reuses (registry Alloc), so a
  // flapping leaf would restart its count from zero every time and could run a
  // whole session without ever reaching the threshold. A flapping limb is
  // exactly the case this warning exists for.
  std::unordered_map<std::string, std::uint64_t> unprimed_video_frames_;

  std::int64_t part_max_video_ts_ = INT64_MIN;
};

}  // namespace visio_schema::mcap
