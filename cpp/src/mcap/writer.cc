#include "visio_schema/mcap/writer.hpp"

#include <fcntl.h>
#include <unistd.h>

#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>

// Vendored header-only mcap, lz4/zstd compiled out (we only ever use
// Compression::None) so this links with no extra deps and cross-compiles for
// the device. MCAP_IMPLEMENTATION pulls the writer .inl into this single TU.
#define MCAP_COMPRESSION_NO_LZ4
#define MCAP_COMPRESSION_NO_ZSTD
#define MCAP_IMPLEMENTATION
#include <mcap/writer.hpp>

namespace visio_schema::mcap {

namespace {

::mcap::McapWriterOptions MakeOptions() {
  ::mcap::McapWriterOptions opts("");  // empty profile: plain protobuf channels
  opts.compression = ::mcap::Compression::None;
  // No chunk CRC: upstream's default CRC-32s every recorded byte inside the
  // chunk writer — ~2 MB/s of table-driven checksumming on the device.
  // Nothing in the pipeline ever verified it (readers default to skipping
  // CRC validation, and repair/finalization checks work on record framing),
  // so the deliberate trade is: no at-rest integrity check inside chunks —
  // truncation is still caught by the footer/summary check, and the dominant
  // payload (H.265) is loudly corrupt on decode. Chunking itself stays ON so
  // readers keep the per-chunk message index for seeking; the summary CRC
  // stays ON (one cheap pass at close).
  opts.noChunkCRC = true;
  return opts;
}

// The protobuf full name Foxglove uses for H.265 video (a camera channel's
// Channel::schema_name). Only these channels are keyframe-gated; audio
// ("foxglove.RawAudio"), IMU, encoder and control are written unconditionally.
constexpr const char* kCompressedVideoSchema = "foxglove.CompressedVideo";

// A drop-in for upstream mcap's FileWriter that opens the part file with
// O_CLOEXEC. Upstream FileWriter uses fopen(path, "wb"), whose fd is NOT
// close-on-exec, so it leaks into every child this process fork+execs — notably
// the long-lived Wi-Fi AP daemons (hostapd/udhcpd/mdnsd) spawned via
// posix_spawn. An inherited recording fd keeps /mnt/sdcard busy for that
// daemon's entire lifetime, so a subsequent `umount` returns EBUSY and the
// "format SD card" command aborts with "still mounted". O_CLOEXEC is the
// race-free fix (marking the fd atomically at open); closing fds in the child
// after posix_spawn is not, in a multithreaded process. Semantics otherwise
// mirror FileWriter exactly (buffered fwrite via fdopen, fclose on end()).
class CloexecFileWriter final : public ::mcap::IWritable {
 public:
  ~CloexecFileWriter() override { end(); }

  ::mcap::Status open(const std::string& filename) {
    end();
    const int fd =
        ::open(filename.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) {
      return ::mcap::Status(::mcap::StatusCode::OpenFailed,
                            "failed to open file \"" + filename +
                                "\" for writing: " + std::strerror(errno));
    }
    file_ = ::fdopen(fd, "wb");
    if (!file_) {
      const std::string msg = "fdopen failed for \"" + filename +
                              "\": " + std::strerror(errno);
      ::close(fd);
      return ::mcap::Status(::mcap::StatusCode::OpenFailed, msg);
    }
    // 256 KiB stdio buffer instead of the default (st_blksize, ~4 KiB): the
    // recorder streams ~1 MB/s to SD through fwrite, and 4 KiB buffering
    // makes every chunk hundreds of small write(2)s on a saturated single
    // core. One part is open at a time, so this is a bounded one-buffer
    // cost. Loss window on power cut grows to ≤256 KiB of tail — the torn
    // part is already mcap_repair territory either way. Failure only costs
    // the optimization (default buffering stands), but say so — a silently
    // absent buffer looks exactly like the fix not working.
    if (::setvbuf(file_, nullptr, _IOFBF, 256 * 1024) != 0) {
      std::fprintf(stderr, "mcap: setvbuf(256KiB) failed — default buffering\n");
    }
    return ::mcap::StatusCode::Success;
  }

  void handleWrite(const std::byte* data, uint64_t size) override {
    if (file_) {
      std::fwrite(data, 1, size, file_);
      size_ += size;
    }
  }

  void end() override {
    if (file_) {
      std::fclose(file_);
      file_ = nullptr;
    }
    size_ = 0;
  }

  uint64_t size() const override { return size_; }

 private:
  std::FILE* file_ = nullptr;
  uint64_t size_ = 0;
};

// Insert "_NNNN" before the file extension: run.mcap -> run_0000.mcap.
// 4-digit zero-pad: parts stay lexicographically ordered through 9999. (At 3
// digits, part 1000 -> "_1000" sorts before "_999", breaking the chronological
// order the uploader/playback rely on once a session exceeds 999 parts.)
std::string NumberedPart(const std::string& path, std::size_t index) {
  char tag[16];
  std::snprintf(tag, sizeof(tag), "_%04zu", index);
  const std::size_t slash = path.find_last_of('/');
  const std::size_t dot = path.find_last_of('.');
  const bool has_ext = dot != std::string::npos &&
                       (slash == std::string::npos || dot > slash);
  if (!has_ext) return path + tag;
  return path.substr(0, dot) + tag + path.substr(dot);
}

// fsync a path (a file, or a directory with O_DIRECTORY) to push it to physical
// media. Reopening read-only is enough — fsync flushes dirty pages regardless of
// the open mode. Best-effort: a failure means the just-finished recording may
// not survive an immediate power-down, so it is logged with that implication
// (the device log is where storage degradation already surfaces, cf.
// McapWriterEndpoint::NoteDrop) but never thrown — the file is already finalized
// on disk, and turning that into an exception on the stop path would be strictly
// worse.
void FsyncPathBestEffort(const std::string& path, int extra_open_flags) {
  const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | extra_open_flags);
  if (fd < 0) {
    std::fprintf(stderr,
                 "McapWriter: cannot open %s to fsync (data may not be "
                 "durable): %s\n",
                 path.c_str(), std::strerror(errno));
    return;
  }
  if (::fsync(fd) != 0) {
    std::fprintf(stderr,
                 "McapWriter: fsync %s failed (data may not be durable): %s\n",
                 path.c_str(), std::strerror(errno));
  }
  ::close(fd);
}

// Push a finished MCAP part's data — and the directory entry recording it —
// onto physical media. The upstream writer's close() ends in fclose(), which
// only flushes stdio buffers into the kernel page cache; on the async-mounted
// SD card a power-down within the writeback window (~30 s) would otherwise
// truncate or corrupt the just-finalized file. fsync the file (its data + size)
// and then the containing directory so the entry is durable too.
void FsyncPart(const std::string& path) {
  FsyncPathBestEffort(path, 0);

  const std::size_t slash = path.find_last_of('/');
  std::string dir;
  if (slash == std::string::npos) {
    dir = ".";
  } else if (slash == 0) {
    dir = "/";
  } else {
    dir = path.substr(0, slash);
  }
  FsyncPathBestEffort(dir, O_DIRECTORY);
}

}  // namespace

McapWriter::McapWriter(std::string_view path, std::uint64_t max_bytes,
                       double max_duration_s, bool rotate_on_keyframe,
                       std::int64_t pair_guard_ns)
    : base_path_(path),
      max_bytes_(max_bytes),
      max_duration_ns_(static_cast<std::int64_t>(max_duration_s * 1e9)),
      rotating_(max_bytes > 0 || max_duration_s > 0.0),
      rotate_on_keyframe_(rotate_on_keyframe),
      pair_guard_ns_(pair_guard_ns) {
  OpenPart();
}

McapWriter::~McapWriter() {
  Close();
}

std::string McapWriter::PartPath() const {
  return rotating_ ? NumberedPart(base_path_, part_index_) : base_path_;
}

void McapWriter::OpenPart() {
  // Each part re-registers its own schemas/channels so it stands alone.
  schema_ids_.clear();
  channel_ids_.clear();
  // Each part must open every video stream on its own keyframe (the previous
  // part's IDR is not in this file), so unprime all video channels and forget the
  // prior part's video high-water timestamp.
  primed_video_channels_.clear();
  part_max_video_ts_ = INT64_MIN;
  part_bytes_ = 0;
  part_start_ = std::chrono::steady_clock::now();
  const std::string p = PartPath();

  // Own the fd (O_CLOEXEC) via our IWritable instead of upstream's fopen(),
  // then hand it to the writer through the open(IWritable&) overload. See
  // CloexecFileWriter for why (recording fds must not leak into forked Wi-Fi
  // daemons). The writable is stored in file_ (declared before writer_) so it
  // outlives the writer that holds a raw pointer to it.
  auto fw = std::make_unique<CloexecFileWriter>();
  const ::mcap::Status status = fw->open(p);
  if (!status.ok()) {
    throw std::runtime_error("McapWriter: cannot open " + p + ": " +
                             status.message);
  }
  writer_ = std::make_unique<::mcap::McapWriter>();
  writer_->open(*fw, MakeOptions());
  file_ = std::move(fw);
  WriteStoredMetadata();  // re-emit capture metadata so each part stands alone
}

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
    std::fprintf(stderr, "McapWriter: metadata record write failed: %s\n",
                 st.message.c_str());
}

bool McapWriter::ShouldRoll() const {
  // Never roll an empty part (a stale duration would spin out empty files).
  if (part_bytes_ == 0) return false;
  if (max_bytes_ > 0 && part_bytes_ >= max_bytes_) return true;
  if (max_duration_ns_ > 0) {
    const auto elapsed = std::chrono::steady_clock::now() - part_start_;
    const auto ns =
        std::chrono::duration_cast<std::chrono::nanoseconds>(elapsed).count();
    if (ns >= max_duration_ns_) return true;
  }
  return false;
}

void McapWriter::CloseCurrentPart() {
  const std::string p = PartPath();  // capture before close, while state is live
  writer_->close();
  FsyncPart(p);
}

void McapWriter::Roll() {
  CloseCurrentPart();
  ++part_index_;
  OpenPart();
}

void McapWriter::Write(const Channel& channel, const Message& msg) {
  if (closed_) return;

  const bool is_video = channel.schema_name == kCompressedVideoSchema;
  const std::int64_t msg_ts_ns = TimestampNs(msg.timestamp);

  if (rotate_on_keyframe_ && rotating_) {
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
    } else if (max_bytes_ > 0 && part_bytes_ >= max_bytes_ + max_bytes_ / 8) {
      Roll();
    }
  } else if (rotating_ && ShouldRoll()) {
    Roll();  // opt-in off: original byte-exact roll
  }

  // Keyframe gate: drop a video channel's frames until its first keyframe of the
  // current part, so every part (first and each rotation) opens each video stream
  // on a decodable IDR. The dropped pre-keyframe P-frames reference a frame in the
  // previous part and are already undecodable in isolation — nothing decodable is
  // lost, only dead bytes trimmed. ShouldRoll() never rolls while part_bytes_ == 0,
  // so a part still awaiting its first keyframe cannot spuriously roll.
  if (is_video &&
      primed_video_channels_.find(channel.id) == primed_video_channels_.end()) {
    if (!msg.keyframe) return;  // pre-keyframe P-frame — drop, don't count
    primed_video_channels_.insert(channel.id);
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
  writer_->write(out);
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
  closed_ = true;
  if (writer_) CloseCurrentPart();
}

}  // namespace visio_schema::mcap
