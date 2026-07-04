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
// the RV1106. MCAP_IMPLEMENTATION pulls the writer .inl into this single TU.
#define MCAP_COMPRESSION_NO_LZ4
#define MCAP_COMPRESSION_NO_ZSTD
#define MCAP_IMPLEMENTATION
#include <mcap/writer.hpp>

namespace visio_schema::mcap {

namespace {

::mcap::McapWriterOptions MakeOptions() {
  ::mcap::McapWriterOptions opts("");  // empty profile: plain protobuf channels
  opts.compression = ::mcap::Compression::None;
  return opts;
}

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
                       double max_duration_s)
    : base_path_(path),
      max_bytes_(max_bytes),
      max_duration_ns_(static_cast<std::int64_t>(max_duration_s * 1e9)),
      rotating_(max_bytes > 0 || max_duration_s > 0.0) {
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

  if (rotating_ && ShouldRoll()) Roll();

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

  const auto ts = static_cast<::mcap::Timestamp>(TimestampNs(msg.timestamp));
  ::mcap::Message out;
  out.channelId = cit->second;
  out.sequence = msg.seq;
  out.logTime = ts;
  out.publishTime = ts;
  out.dataSize = msg.payload.size();
  out.data = reinterpret_cast<const std::byte*>(msg.payload.data());
  writer_->write(out);
  part_bytes_ += msg.payload.size();
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
