#include "visio_schema/mcap/writer.hpp"

#include "visio_schema/mcap/recording_crypto.hpp"

#include <fcntl.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <algorithm>
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
// ~5 s at 60 fps, 10 s at 30 — well past any real GOP wait, and early enough
// to fire inside a short session.
constexpr std::uint64_t kUnprimedVideoWarnFrames = 300;

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
  // sync_span_bytes > 0: see SyncSpan() below for the full mechanism.
  // Spans below one page would stop advancing past the aligned boundary
  // and degrade into an fflush per write; clamp rather than trust callers.
  // `key`, when set, makes each part a VREC container: a 32-byte plaintext
  // header, then the MCAP stream under ChaCha20. size() keeps reporting the
  // PLAINTEXT length — that is what MCAP records in its index, and it is what
  // makes the container's offset identity (file offset = plaintext + 32) hold.
  explicit CloexecFileWriter(std::uint64_t sync_span_bytes = 0,
                             const RecordingKey* key = nullptr)
      : sync_span_bytes_(
            sync_span_bytes ? std::max<std::uint64_t>(sync_span_bytes, 4096)
                            : 0),
        encrypting_(key != nullptr) {
    if (key) key_ = *key;
  }
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
    fd_ = fd;
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
    if (encrypting_) {
      const ::mcap::Status st = BeginVrec(filename);
      if (!st.ok()) {
        end();
        return st;
      }
    }
    return ::mcap::StatusCode::Success;
  }

  void handleWrite(const std::byte* data, uint64_t size) override {
    if (!file_) return;
    if (!cipher_) {
      WriteBytes(data, size);
      return;
    }
    // Encrypt through a fixed scratch buffer rather than in place: `data` is
    // the caller's, and mcap hands us spans larger than one buffer, so slice.
    const auto* src = reinterpret_cast<const std::uint8_t*>(data);
    for (uint64_t done = 0; done < size;) {
      const size_t n =
          static_cast<size_t>(std::min<uint64_t>(size - done, scratch_.size()));
      // Keystream position is the PLAINTEXT offset, which is size_ — so a
      // short write leaves the next call correctly positioned.
      if (!cipher_->XorAt(size_, src + done, n, scratch_.data())) {
        if (!write_err_logged_) {
          write_err_logged_ = true;
          std::fprintf(stderr, "mcap: VREC encrypt failed at offset %llu\n",
                       static_cast<unsigned long long>(size_));
        }
        return;
      }
      const size_t landed =
          WriteBytes(reinterpret_cast<const std::byte*>(scratch_.data()), n);
      done += landed;
      if (landed != n) return;  // short write already reported
    }
  }

  // Raw path: fwrite, short-write accounting, writeback span. Returns what
  // actually landed. `size_` is the PLAINTEXT total either way.
  size_t WriteBytes(const std::byte* data, uint64_t size) {
    const size_t wrote = std::fwrite(data, 1, size, file_);
    // A short write is an SD/ENOSPC failure stdio may otherwise defer to
    // fclose; a silently short part would still carry plausible offsets.
    // Advance by what actually landed so every downstream offset (sync
    // spans included) matches the file, and say so once.
    if (wrote != size && !write_err_logged_) {
      write_err_logged_ = true;
      std::fprintf(stderr, "mcap: short write (%zu of %llu): %s\n", wrote,
                   static_cast<unsigned long long>(size),
                   std::strerror(errno));
    }
    size_ += wrote;
    if (sync_span_bytes_ > 0 && file_size() - synced_off_ >= sync_span_bytes_)
      SyncSpan();
    return wrote;
  }

  void end() override {
    if (file_) {
      std::fclose(file_);
      file_ = nullptr;
    }
    fd_ = -1;
    cipher_.reset();
    header_bytes_ = 0;
    size_ = 0;
    synced_off_ = 0;
    prev_off_ = 0;
    prev_len_ = 0;
    sync_disabled_ = false;
    write_err_logged_ = false;
  }

  uint64_t size() const override { return size_; }

 private:
  // File offset = plaintext offset + any VREC header. SyncSpan and fadvise
  // address the FILE; MCAP's index addresses the plaintext. They differ by
  // exactly 32 bytes on an encrypted part, and conflating the two would skew
  // every writeback span by that much.
  uint64_t file_size() const { return size_ + header_bytes_; }

  // Mint a nonce, write the 32-byte plaintext header, arm the cipher. Called
  // from open() before any MCAP byte reaches the file.
  ::mcap::Status BeginVrec(const std::string& filename) {
    RecordingNonce nonce{};
    if (!RandomNonce(&nonce)) {
      // Never fall back to a fixed nonce: two parts sharing key+nonce share a
      // keystream, and XORing them recovers both plaintexts with no key.
      return ::mcap::Status(::mcap::StatusCode::OpenFailed,
                            "VREC: no CSPRNG for a part nonce, refusing to "
                            "encrypt \"" +
                                filename + "\"");
    }
    VrecHeader header;
    header.key_fp = RecordingKeyFingerprint(key_);
    header.nonce = nonce;
    std::array<std::uint8_t, kVrecHeaderBytes> raw{};
    WriteVrecHeader(header, raw.data());
    if (std::fwrite(raw.data(), 1, raw.size(), file_) != raw.size()) {
      return ::mcap::Status(::mcap::StatusCode::OpenFailed,
                            "VREC: cannot write header to \"" + filename +
                                "\": " + std::strerror(errno));
    }
    header_bytes_ = raw.size();
    auto cipher = std::make_unique<RecordingCipher>(key_, nonce);
    if (!cipher->valid()) {
      return ::mcap::Status(
          ::mcap::StatusCode::OpenFailed,
          "VREC: cipher init failed for \"" + filename + "\"");
    }
    cipher_ = std::move(cipher);
    return ::mcap::StatusCode::Success;
  }

  const bool encrypting_ = false;
  RecordingKey key_{};
  std::unique_ptr<RecordingCipher> cipher_;
  uint64_t header_bytes_ = 0;
  // Fixed, so encryption never adds an allocation to the recorder's write
  // path. 64 KiB covers a typical chunk in one pass; larger spans just loop.
  std::array<std::uint8_t, 64 * 1024> scratch_{};

  // uClibc-ng marshals sync_file_range() WRONG on 32-bit ARM: the kernel's
  // only ARM entry point is arm_sync_file_range (= sync_file_range2, flags
  // in r1 per the EABI's even-register rule for 64-bit args), but the libc
  // stub passes the generic order — the kernel reads flags = 0 and the call
  // is a successful no-op. Verified by disassembly of the SDK toolchain's
  // libc.so.1 (its posix_fadvise64 does the ARM swizzle correctly; this one
  // doesn't). Issue the syscall ourselves on ARM; syscall(2) passes longs
  // in r0..r5, which is exactly sync_file_range2's layout.
  static long sync_range(int fd, uint64_t off, uint64_t len,
                         unsigned int flags) {
#if defined(__linux__) && defined(__arm__)
#ifndef __NR_sync_file_range2
#error "32-bit ARM without sync_file_range2 would fall back into the broken libc stub"
#endif
    static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,
                  "the lo/hi register split below assumes little-endian");
    return ::syscall(__NR_sync_file_range2, fd, flags,
                     static_cast<unsigned long>(off & 0xffffffffu),
                     static_cast<unsigned long>(off >> 32),
                     static_cast<unsigned long>(len & 0xffffffffu),
                     static_cast<unsigned long>(len >> 32));
#elif defined(__linux__)
    return ::sync_file_range(fd, static_cast<off64_t>(off),
                             static_cast<off64_t>(len), flags);
#else
    (void)fd;
    (void)off;
    (void)len;
    (void)flags;
    return 0;
#endif
  }

  // Hand the span written since the last call to kernel writeback, and wait
  // out + evict the span before it, so the file's dirty set stays bounded
  // near one span (plus the partial tail page, deliberately kept — fadvise
  // ignores partial pages, and the next fwrite re-dirties it anyway). The
  // alternative is the kernel's dirty-page throttling, which lets hundreds
  // of MB accumulate and then stalls a write() for hundreds of ms at a
  // moment of its choosing. Waiting one span BEHIND keeps this call ~free
  // while storage keeps up (the span in flight had a full fill period of
  // head start) and bounds the stall to one span's write time when it does
  // not. WAIT_BEFORE|WRITE|WAIT_AFTER on the previous span is deliberate:
  // only the triple upgrades to WB_SYNC_ALL, guaranteeing the span is clean
  // before DONTNEED (which silently skips dirty pages). Failures are
  // best-effort — the cost is only this optimization — but say so once per
  // part: a silently absent sync looks exactly like the fix not working
  // (same argument as setvbuf above).
  void SyncSpan() {
    // 4 KiB on every kernel we ship (RV1106/RV1126B); a larger-page target
    // would only strand a few clean pages per span, not corrupt anything.
    constexpr uint64_t kPageMask = 4095;
    if (std::fflush(file_) != 0) {
      if (!sync_disabled_) {
        sync_disabled_ = true;
        std::fprintf(stderr,
                     "mcap: fflush in SyncSpan failed (%s) — dirty-set "
                     "bounding disabled for this part\n",
                     std::strerror(errno));
      }
      // Aligned like the happy path, or the next span's fadvise would round
      // the unaligned start UP and strand the straddling page for good.
      synced_off_ = file_size() & ~kPageMask;
      prev_len_ = 0;
      return;
    }
#if defined(__linux__)
    if (sync_disabled_) {
      synced_off_ = file_size() & ~kPageMask;
      return;
    }
    // Page-align the span end: fadvise rounds partial pages AWAY, so an
    // unaligned boundary would strand one straddling page per span in the
    // cache forever. The partial tail waits for the next span.
    const uint64_t end = file_size() & ~kPageMask;
    if (end <= synced_off_) return;
    const uint64_t off = synced_off_;
    const uint64_t len = end - synced_off_;
    long rc = sync_range(fd_, off, len, SYNC_FILE_RANGE_WRITE);
    int fadvise_err = 0;
    if (rc == 0 && prev_len_ > 0) {
      rc = sync_range(fd_, prev_off_, prev_len_,
                      SYNC_FILE_RANGE_WAIT_BEFORE | SYNC_FILE_RANGE_WRITE |
                          SYNC_FILE_RANGE_WAIT_AFTER);
      // posix_fadvise returns its error and does NOT set errno.
      if (rc == 0)
        fadvise_err = ::posix_fadvise64(fd_, static_cast<off64_t>(prev_off_),
                                        static_cast<off64_t>(prev_len_),
                                        POSIX_FADV_DONTNEED);
    }
    if ((rc != 0 || fadvise_err != 0) && !sync_disabled_) {
      sync_disabled_ = true;
      std::fprintf(stderr,
                   "mcap: span writeback failed (%s) — dirty-set bounding "
                   "disabled for this part\n",
                   std::strerror(fadvise_err != 0 ? fadvise_err : errno));
    }
    prev_off_ = off;
    prev_len_ = len;
    synced_off_ = end;
#else
    synced_off_ = file_size() & ~kPageMask;
#endif
  }

  const std::uint64_t sync_span_bytes_;
  std::FILE* file_ = nullptr;
  int fd_ = -1;
  uint64_t size_ = 0;
  uint64_t synced_off_ = 0;
  uint64_t prev_off_ = 0;
  uint64_t prev_len_ = 0;
  bool sync_disabled_ = false;
  bool write_err_logged_ = false;
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
                       std::int64_t pair_guard_ns,
                       std::uint64_t sync_span_bytes,
                       std::optional<RecordingKey> recording_key)
    : base_path_(path),
      max_bytes_(max_bytes),
      max_duration_ns_(static_cast<std::int64_t>(max_duration_s * 1e9)),
      rotating_(max_bytes > 0 || max_duration_s > 0.0),
      rotate_on_keyframe_(rotate_on_keyframe),
      pair_guard_ns_(pair_guard_ns),
      sync_span_bytes_(sync_span_bytes),
      recording_key_(std::move(recording_key)) {
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
  auto fw = std::make_unique<CloexecFileWriter>(
      sync_span_bytes_, recording_key_ ? &*recording_key_ : nullptr);
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
    if (!msg.keyframe) {
      // Normally a handful of frames until the next IDR (GOP is one second).
      // A channel that NEVER primes is a different animal: its whole topic is
      // absent from the recording and nothing else reports it. That is what a
      // relayed leaf looked like before Header.keyframe was serialized, and it
      // is what a rig running mixed firmware still looks like — a leaf too old
      // to set the wire flag can never prime. Warn once per channel, well past
      // any legitimate wait.
      auto& n = unprimed_video_frames_[channel.topic];
      if (++n == kUnprimedVideoWarnFrames) {
        std::fprintf(stderr,
                     "McapWriter: %s has sent %llu video frames with no "
                     "keyframe — the whole topic is being dropped from this "
                     "recording. If it is relayed, its device may predate the "
                     "wire keyframe flag.\n",
                     channel.topic.c_str(),
                     static_cast<unsigned long long>(n));
      }
      return;  // pre-keyframe P-frame — drop, don't count
    }
    primed_video_channels_.insert(channel.id);
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
