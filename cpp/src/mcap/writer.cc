#include "visio_schema/mcap/writer.hpp"

#include "visio_schema/log.hpp"
#include "visio_schema/mcap/recording_crypto.hpp"

#include <fcntl.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <string>
#include <utility>

#include "file_sync.hpp"
#include "span_readback.hpp"

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
  // `readback`, when set, receives every byte as it lands and every span
  // as it is evicted (write-time read-back; see span_readback.hpp).
  explicit CloexecFileWriter(std::uint64_t sync_span_bytes = 0,
                             const RecordingKey* key = nullptr,
                             SpanReadback* readback = nullptr)
      : encrypting_(key != nullptr),
        readback_(readback),
        sync_span_bytes_(
            sync_span_bytes ? std::max<std::uint64_t>(sync_span_bytes,
                                                      file_sync::kPageBytes)
                            : 0) {
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
      log::Write(log::Severity::kWarning,
                 "mcap: setvbuf(256KiB) failed — default buffering");
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
          log::Write(log::Severity::kError,
                     "mcap: VREC encrypt failed at offset %llu",
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
      log::Write(log::Severity::kError, "mcap: short write (%zu of %llu): %s",
                 wrote, static_cast<unsigned long long>(size),
                 std::strerror(errno));
    }
    // The read-back's copy of what just landed, keyed by FILE offset —
    // below the cipher, so it is the bytes as they are on disk. This
    // memcpy is the read-back's entire cost on the write path.
    if (readback_) readback_->OnBytesWritten(file_size(), data, wrote);
    size_ += wrote;
    if (sync_span_bytes_ > 0 && file_size() - synced_off_ >= sync_span_bytes_)
      SyncSpan();
    return wrote;
  }

  void end() override {
    if (file_) {
      const uint64_t final_size = file_size();
      std::fclose(file_);
      file_ = nullptr;
      // Everything past the last evicted span — the span synced but not
      // yet waited on, plus the partial tail — is on disk now but not
      // verified; the owner posts it once the part is fsynced.
      if (readback_)
        readback_->NotePartEnd(evicted_end_, final_size, sync_disabled_);
    }
    fd_ = -1;
    cipher_.reset();
    header_bytes_ = 0;
    size_ = 0;
    synced_off_ = 0;
    prev_off_ = 0;
    prev_len_ = 0;
    evicted_end_ = 0;
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
    // The header is on-disk bytes like any other: the read-back compares
    // the file from offset 0.
    if (readback_) readback_->OnBytesWritten(0, raw.data(), raw.size());
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
  // Write-time read-back sink; null when off. Owned by McapWriter, which
  // outlives this writable.
  SpanReadback* const readback_;
  RecordingKey key_{};
  std::unique_ptr<RecordingCipher> cipher_;
  uint64_t header_bytes_ = 0;
  // Fixed, so encryption never adds an allocation to the recorder's write
  // path. 64 KiB covers a typical chunk in one pass; larger spans just loop.
  std::array<std::uint8_t, 64 * 1024> scratch_{};

  // Hand the span written since the last call to kernel writeback, and wait
  // out + evict the span before it, so the file's dirty set stays bounded
  // near one span (plus the partial tail page, deliberately kept — fadvise
  // ignores partial pages, and the next fwrite re-dirties it anyway). The
  // alternative is the kernel's dirty-page throttling, which lets hundreds
  // of MB accumulate and then stalls a write() for hundreds of ms at a
  // moment of its choosing. Waiting one span BEHIND keeps this call ~free
  // while storage keeps up (the span in flight had a full fill period of
  // head start) and bounds the stall to one span's write time when it does
  // not. The previous span is waited on and evicted in one go
  // (file_sync::WritebackAndEvict explains the flag triple). Failures are
  // best-effort — the cost is only this optimization — but say so once per
  // part: a silently absent sync looks exactly like the fix not working
  // (same argument as setvbuf above).
  void SyncSpan() {
    // 4 KiB on every kernel we ship (RV1106/RV1126B); a larger-page target
    // would only strand a few clean pages per span, not corrupt anything.
    if (std::fflush(file_) != 0) {
      if (!sync_disabled_) {
        sync_disabled_ = true;
        log::Write(log::Severity::kWarning,
                   "mcap: fflush in SyncSpan failed (%s) — dirty-set bounding "
                   "disabled for this part",
                   std::strerror(errno));
        if (readback_) readback_->OnSyncDisabled();
      }
      // Aligned like the happy path, or the next span's fadvise would round
      // the unaligned start UP and strand the straddling page for good.
      synced_off_ = file_sync::RoundDownToPage(file_size());
      prev_len_ = 0;
      return;
    }
#if defined(__linux__)
    if (sync_disabled_) {
      synced_off_ = file_sync::RoundDownToPage(file_size());
      return;
    }
    // Page-align the span end: fadvise rounds partial pages AWAY, so an
    // unaligned boundary would strand one straddling page per span in the
    // cache forever. The partial tail waits for the next span.
    const uint64_t end = file_sync::RoundDownToPage(file_size());
    if (end <= synced_off_) return;
    const uint64_t off = synced_off_;
    const uint64_t len = end - synced_off_;
    int err = 0;
    if (file_sync::SyncRange(fd_, off, len, SYNC_FILE_RANGE_WRITE) != 0)
      err = errno;
    if (err == 0 && prev_len_ > 0) err = EvictPreviousSpan();
    if (err != 0 && !sync_disabled_) {
      sync_disabled_ = true;
      log::Write(
          log::Severity::kWarning,
          "mcap: span writeback failed (%s) — dirty-set bounding disabled "
          "for this part",
          std::strerror(err));
      if (readback_) readback_->OnSyncDisabled();
    }
    prev_off_ = off;
    prev_len_ = len;
    synced_off_ = end;
#else
    synced_off_ = file_sync::RoundDownToPage(file_size());
#endif
  }

  const std::uint64_t sync_span_bytes_;
  std::FILE* file_ = nullptr;
  int fd_ = -1;
  uint64_t size_ = 0;
  // Wait out and evict the span before the one just handed to writeback.
  // The one point where a span is known written back AND out of the page
  // cache: the medium now holds the only copy outside the read-back ring,
  // so this is where it is queued for reading. Returns 0 or errno.
  int EvictPreviousSpan() {
    const int err = file_sync::WritebackAndEvict(fd_, prev_off_, prev_len_);
    if (err != 0) return err;
    evicted_end_ = prev_off_ + prev_len_;
    if (readback_) readback_->OnSpanEvicted(prev_off_, prev_len_);
    return 0;
  }

  uint64_t synced_off_ = 0;
  uint64_t prev_off_ = 0;
  uint64_t prev_len_ = 0;
  // End of the last span written back + evicted: equals prev_off_ while
  // spans stay contiguous, kept explicit for the part-end accounting.
  uint64_t evicted_end_ = 0;
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

}  // namespace

McapWriter::McapWriter(std::string_view path, std::uint64_t max_bytes,
                       double max_duration_s, bool rotate_on_keyframe,
                       std::int64_t pair_guard_ns,
                       std::uint64_t sync_span_bytes,
                       std::optional<RecordingKey> recording_key,
                       McapReadbackOptions readback)
    : base_path_(path),
      max_bytes_(max_bytes),
      max_duration_ns_(static_cast<std::int64_t>(max_duration_s * 1e9)),
      rotating_(max_bytes > 0 || max_duration_s > 0.0),
      rotate_on_keyframe_(rotate_on_keyframe),
      pair_guard_ns_(pair_guard_ns),
      sync_span_bytes_(sync_span_bytes),
      recording_key_(std::move(recording_key)),
      readback_(readback.ring_bytes > 0
                    ? std::make_unique<SpanReadback>(std::move(readback))
                    : nullptr) {
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
  // The read-back learns the part before its first byte (the VREC header
  // is written inside open()).
  if (readback_) readback_->BeginPart(p);
  auto fw = std::make_unique<CloexecFileWriter>(
      sync_span_bytes_, recording_key_ ? &*recording_key_ : nullptr,
      readback_.get());
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
    log::Write(log::Severity::kError,
               "McapWriter: metadata record write failed: %s",
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
  // A part is opened eagerly the moment its predecessor crosses the cap, so one
  // can close having never accepted a record: the keyframe gate below drops the
  // very frame that triggered the roll, and every frame after it, until that
  // stream's next IDR — and a write path that fails leaves nothing at all, not
  // even the header. Either way the file carries no records, which no reader
  // will open, sitting in a session directory that is otherwise sound. Drop it
  // instead of shipping it.
  //
  // Part 0 is exempt: it is the only part a short session has, and a session
  // directory holding no recording at all is a worse thing to hand a reader
  // than an empty but well-formed file.
  const bool drop_part = part_index_ > 0 && part_bytes_ == 0;
  writer_->close();
  if (drop_part) {
    // Before any fsync or read-back post: syncing a file about to be unlinked
    // only makes the discarded state durable, and a tail posted here names a
    // path that no longer exists by the time the reader reaches it (an ENOENT
    // counted as a read failure). Finish() accounts for an unposted tail.
    if (std::remove(p.c_str()) != 0 && errno != ENOENT) {
      // Best-effort but never silent, the same contract FsyncPathBestEffort
      // keeps: on a card that has gone read-only this is the one failure that
      // puts the artifact back, so it says what shipped and why.
      log::Write(log::Severity::kWarning,
                 "McapWriter: cannot remove empty part %s (it will ship in the "
                 "session): %s",
                 p.c_str(), std::strerror(errno));
    }
    file_sync::FsyncDirEntry(p);  // the REMOVAL is what has to survive a cut
    return;
  }
  file_sync::FsyncPart(p);
  // Only now is the tail on the media: post it for read-back.
  if (readback_) readback_->CommitTail();
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
        log::Write(
            log::Severity::kWarning,
            "McapWriter: %s has sent %llu video frames with no keyframe — "
            "the whole topic is being dropped from this recording. If it is "
            "relayed, its device may predate the wire keyframe flag.",
            channel.topic.c_str(), static_cast<unsigned long long>(n));
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
  // Bounded by close_flush_ms (0 = none); whatever is left is counted
  // skipped and the ring is released.
  if (readback_) readback_->Finish();
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
  return readback_ && readback_->storage_fault();
}

}  // namespace visio_schema::mcap
