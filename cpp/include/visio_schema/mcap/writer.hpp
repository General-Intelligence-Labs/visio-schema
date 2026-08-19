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
// Rotation: pass max_bytes and/or max_duration_s to split into self-contained
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
#include <memory>
#include <string>
#include <string_view>
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
  // sync_span_bytes (opt-in, 0 = off): hand the part file's bytes to kernel
  // writeback in spans of at least this many bytes as they are written
  // (page-aligned; NOT an MCAP Chunk boundary), keeping the dirty page set
  // bounded instead of letting the kernel accumulate and stall the writer.
  // Linux-only; elsewhere it degrades to a periodic fflush. Output bytes
  // are identical either way. Full rationale at SyncSpan() in writer.cc.
  explicit McapWriter(std::string_view path, std::uint64_t max_bytes = 0,
                      double max_duration_s = 0.0, bool rotate_on_keyframe = false,
                      std::int64_t pair_guard_ns = 0,
                      std::uint64_t sync_span_bytes = 0);
  ~McapWriter();

  McapWriter(const McapWriter&) = delete;
  McapWriter& operator=(const McapWriter&) = delete;

  // Record one message against `channel` (lazily registering its schema +
  // channel records — declare-before-write).
  void Write(const Channel& channel, const Message& msg);
  void Close();

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

 private:
  std::string PartPath() const;
  void OpenPart();          // throws on open failure
  bool ShouldRoll() const;
  void Roll();
  // Close the current part and fsync it to physical media before moving on.
  void CloseCurrentPart();
  // Emit the stored Metadata record into the current part (no-op if none set).
  void WriteStoredMetadata();

  std::string meta_name_;
  std::map<std::string, std::string> meta_;

  const std::string base_path_;
  const std::uint64_t max_bytes_;
  const std::int64_t max_duration_ns_;
  const bool rotating_;
  const bool rotate_on_keyframe_;
  const std::int64_t pair_guard_ns_;
  const std::uint64_t sync_span_bytes_;

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
  // Lifetime total (never reset on rotation); see bytes_written().
  std::atomic<std::uint64_t> bytes_written_{0};
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
  std::int64_t part_max_video_ts_ = INT64_MIN;
};

}  // namespace visio_schema::mcap
