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
  // Chunk CRC stays at upstream's default (on): it is the only check that
  // sees a flipped bit inside a payload. Record framing and the message
  // index survive damage confined to message bodies, so without it a part
  // whose chunks were altered at rest still reads as clean. The CRC is
  // accumulated as each record enters the chunk buffer, while the bytes are
  // still hot in cache.
  return opts;
}

// The part file's stdio buffer. Small on purpose, and the cipher staging
// buffer below must stay STRICTLY larger than it — both reasons are in
// CloexecFileWriter::open() and ::handleWrite(); the invariant is asserted
// under the class.
constexpr std::size_t kStdioBufferBytes = 16 * 1024;
constexpr std::size_t kCipherScratchBytes = 64 * 1024;

// The protobuf full name Foxglove uses for H.265 video (a camera channel's
// Channel::schema_name). Only these channels are keyframe-gated; audio
// ("foxglove.RawAudio"), IMU, encoder and control are written unconditionally.
constexpr const char* kCompressedVideoSchema = "foxglove.CompressedVideo";
// ~5 s at 60 fps, 10 s at 30 — well past any real GOP wait, and early enough
// to fire inside a short session.
constexpr std::uint64_t kUnprimedVideoWarnFrames = 300;

// MCAP framing and VREC encryption use the same checked device backend.
class CloexecFileWriter final : public ::mcap::IWritable {
 public:
  explicit CloexecFileWriter(std::uint64_t sync_span_bytes,
                             const RecordingKey* key, SpanReadback* readback,
                             std::atomic<bool>* fault, IoResult* result,
                             std::shared_ptr<WriterStorage> storage,
                             std::chrono::milliseconds checkpoint_interval)
      : fault_(fault), result_(result), storage_(std::move(storage)),
        checkpoint_interval_(checkpoint_interval), encrypting_(key != nullptr),
        readback_(readback), sync_span_bytes_(sync_span_bytes ?
          std::max<std::uint64_t>(sync_span_bytes, file_sync::kPageBytes) : 0) {
    if (key) key_ = *key;
  }
  ~CloexecFileWriter() override { end(); }

  ::mcap::Status open(const std::string& filename) {
    IoResult result;
    file_ = storage_->Create(filename, result);
    if (!file_) return ::mcap::Status(::mcap::StatusCode::OpenFailed,
                                      std::strerror(result.error));
    checkpoint_at_ = std::chrono::steady_clock::now();
    if (!encrypting_) return ::mcap::StatusCode::Success;
    const auto status = BeginVrec(filename);
    if (!status.ok()) end();
    return status;
  }

  void handleWrite(const std::byte* data, uint64_t size) override {
    if (!file_ || (fault_ && fault_->load(std::memory_order_relaxed))) return;
    if (!cipher_) {
      WriteBytes(data, size);
      return;
    }
    // Encrypt through a staging buffer rather than in place: `data` is the
    // caller's, and mcap hands us spans larger than one buffer, so slice. The
    // slice size is over the stdio buffer by construction, so each fwrite
    // below goes straight to write(2) — this XOR is the only copy encryption
    // adds (see open()).
    const auto* src = reinterpret_cast<const std::uint8_t*>(data);
    for (uint64_t done = 0; done < size;) {
      const size_t n = static_cast<size_t>(
          std::min<uint64_t>(size - done, kCipherScratchBytes));
      // Keystream position is the PLAINTEXT offset, which is size_ — so a
      // short write leaves the next call correctly positioned.
      if (!cipher_->XorAt(size_, src + done, n, scratch_.get())) {
        NoteFault();
        if (!write_err_logged_) {
          write_err_logged_ = true;
          log::Write(log::Severity::kError,
                     "mcap: VREC encrypt failed at offset %llu",
                     static_cast<unsigned long long>(size_));
        }
        return;
      }
      const size_t landed =
          WriteBytes(reinterpret_cast<const std::byte*>(scratch_.get()), n);
      done += landed;
      if (landed != n) return;  // short write already reported
    }
  }

  // Raw path: fwrite, short-write accounting, writeback span. Returns what
  // actually landed. `size_` is the PLAINTEXT total either way.
  size_t WriteBytes(const std::byte* data, uint64_t size) {
    const auto result = file_->Append(data, static_cast<std::size_t>(size));
    const size_t wrote = result.accepted;
    Check(result);
    // A short write is an SD/ENOSPC failure stdio may otherwise defer to
    // fclose; a silently short part would still carry plausible offsets.
    // Advance by what actually landed so every downstream offset (sync
    // spans included) matches the file, and say so once.
    if (wrote != size) NoteFault();
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
    if (checkpoint_interval_.count() > 0 &&
        std::chrono::steady_clock::now() - checkpoint_at_ >= checkpoint_interval_) {
      Check(file_->Checkpoint());
      checkpoint_at_ = std::chrono::steady_clock::now();
    }
    return wrote;
  }

  void end() override {
    if (file_) {
      const uint64_t final_size = file_size();
      Check(file_->Checkpoint());
      const auto closed = file_->Close();
      Check(closed);
      file_.reset();
      result_->accepted = closed.accepted;
      // Everything past the last evicted span — the span synced but not
      // yet waited on, plus the partial tail — is on disk now but not
      // verified; the owner posts it once the part is fsynced.
      if (readback_)
        readback_->NotePartEnd(evicted_end_, final_size, sync_disabled_);
    }
    cipher_.reset();
    scratch_.reset();  // nothing between parts holds a part's staging buffer
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
  void Check(const IoResult& result) {
    if (!result) {
      if (result_->error == 0) *result_ = result;
      NoteFault();
    }
  }
  void NoteFault() {
    if (result_->error == 0) *result_ = {IoStep::append, EIO, 0, true};
    fault_->store(true, std::memory_order_relaxed);
  }
  std::atomic<bool>* const fault_;
  IoResult* const result_;
  const std::shared_ptr<WriterStorage> storage_;
  const std::chrono::milliseconds checkpoint_interval_;
  std::chrono::steady_clock::time_point checkpoint_at_;

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
    const auto wrote = file_->Append(raw.data(), raw.size());
    Check(wrote);
    if (!wrote) {
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
    // Staged before the cipher is armed, so `cipher_ != nullptr` (what
    // handleWrite branches on) can never outrun the buffer it writes into.
    scratch_ = std::make_unique<std::uint8_t[]>(kCipherScratchBytes);
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
  // Cipher staging. Allocated once per part in BeginVrec() and only there, so
  // a plaintext recording carries none of it — as a member it was 64 KiB that
  // every plaintext part allocated AND zeroed (16 dirty pages) to never touch.
  // Non-null exactly while cipher_ is, and never allocated per write. Its size
  // is kCipherScratchBytes, which must stay above kStdioBufferBytes (see
  // open()); 64 KiB also keeps one chunk blob at a dozen write(2)s.
  std::unique_ptr<std::uint8_t[]> scratch_;

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
  // Unsupported writeback disables the optimization. Other failures belong
  // to the part's final save result even if a later fsync succeeds.
  void SyncSpan() {
    // 4 KiB on every kernel we ship (RV1106/RV1126B); a larger-page target
    // would only strand a few clean pages per span, not corrupt anything.
    const auto flushed = file_->Flush();
    Check(flushed);
    if (!flushed) {
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
    err = file_->Writeback(off, len, false);
    if (err == 0 && prev_len_ > 0) err = EvictPreviousSpan();
    if (err != 0 && err != ENOSYS && err != EINVAL && err != EOPNOTSUPP)
      Check({IoStep::checkpoint, err, 0, true});
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
  std::unique_ptr<OutputFile> file_;
  uint64_t size_ = 0;
  // Wait out and evict the span before the one just handed to writeback.
  // The one point where a span is known written back AND out of the page
  // cache: the medium now holds the only copy outside the read-back ring,
  // so this is where it is queued for reading. Returns 0 or errno.
  int EvictPreviousSpan() {
    const int err = file_->Writeback(prev_off_, prev_len_, true);
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

// The two sizing invariants this writable rests on, pinned where they cannot
// drift: a slice must be an over-size request against the stdio buffer (or
// every encrypted byte is copied into it), and no staging buffer may live
// INLINE here — a plaintext part must carry none at all, which a member array
// would silently undo without allocating anything for a test to notice.
static_assert(kCipherScratchBytes > kStdioBufferBytes,
              "cipher slices must exceed the stdio buffer to bypass it");
static_assert(sizeof(CloexecFileWriter) < 1024,
              "the part writable must stay small: no inline staging buffer");

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
                       McapReadbackOptions readback,
                       std::shared_ptr<WriterStorage> storage,
                       std::chrono::milliseconds checkpoint_interval,
                       std::optional<McapRotationPolicy> rotation_policy)
    : rotation_policy_(std::move(rotation_policy)), base_path_(path),
      max_bytes_(max_bytes),
      max_duration_ns_(static_cast<std::int64_t>(max_duration_s * 1e9)),
      rotating_(max_bytes > 0 || max_duration_s > 0.0),
      rotate_on_keyframe_(rotate_on_keyframe),
      pair_guard_ns_(pair_guard_ns),
      sync_span_bytes_(sync_span_bytes),
      recording_key_(std::move(recording_key)),
      storage_(storage ? std::move(storage) : LocalWriterStorage()),
      checkpoint_interval_(checkpoint_interval),
      readback_(readback.ring_bytes > 0
                    ? std::make_unique<SpanReadback>(std::move(readback))
                    : nullptr) {
  if (rotation_policy_) {
    if (rotation_policy_->tolerance_ns < 0 || rotation_policy_->max_staged_bytes == 0 ||
        rotation_policy_->max_wait.count() < 0)
      throw std::invalid_argument("invalid MCAP rotation policy");
    std::unordered_set<std::uint32_t> ids(rotation_policy_->local_video_ids.begin(),
                                          rotation_policy_->local_video_ids.end());
    if (ids.size() != rotation_policy_->local_video_ids.size())
      throw std::invalid_argument("duplicate local video id");
    RecordingNonce nonce{};
    if (!RandomNonce(&nonce)) throw std::runtime_error("recording identity entropy unavailable");
    recording_id_ = HexOf(nonce.data(), nonce.size());
  }
  OpenPart();
}

McapWriter::~McapWriter() {
  try { Close(); }
  catch (...) { log::Write(log::Severity::kError, "McapWriter: destructor finalize failed"); }
}

std::string McapWriter::PartPath() const {
  return rotating_ ? NumberedPart(base_path_, part_index_) : base_path_;
}

void McapWriter::OpenPart() {
  // Each part re-registers its own schemas/channels so it stands alone.
  schema_ids_.clear();
  channel_ids_.clear();
  // Legacy parts warm up independently. A continuation chain only warms up
  // local cameras once, including cameras whose first IDR arrives after a cut.
  if (!rotation_policy_) primed_video_channels_.clear();
  part_max_video_ts_ = INT64_MIN;
  part_bytes_ = 0;
  part_write_failure_logged_ = false;
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
      readback_.get(), &io_fault_, &last_io_, storage_, checkpoint_interval_);
  const ::mcap::Status status = fw->open(p);
  if (!status.ok()) {
    throw std::runtime_error("McapWriter: cannot open " + p + ": " +
                             status.message);
  }
  writer_ = std::make_unique<::mcap::McapWriter>();
  writer_->open(*fw, MakeOptions());
  file_ = std::move(fw);
  WriteStoredMetadata();  // re-emit capture metadata so each part stands alone
  WriteRotationMetadata();
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
  const bool drop_part = part_index_ > 0 && part_bytes_ == 0 && !io_fault_.load();
  writer_->close();
  physical_closed_ += last_io_.accepted;
  if (drop_part) {
    // Before any fsync or read-back post: syncing a file about to be unlinked
    // only makes the discarded state durable, and a tail posted here names a
    // path that no longer exists by the time the reader reaches it (an ENOENT
    // counted as a read failure). Finish() accounts for an unposted tail.
    const auto removed = storage_->Remove(p);
    const auto synced = removed ? storage_->SyncParent(p) : removed;
    if (!synced) { last_io_ = synced; io_fault_.store(true); }
    return;
  }
  const auto synced = storage_->SyncParent(p);
  if (!synced && last_io_.error == 0) last_io_ = synced;
  if (io_fault_.load() && last_io_.error == 0)
    last_io_ = {IoStep::close, EIO, last_io_.accepted, true};
  last_io_ = storage_->Completed(p, last_io_);
  if (!last_io_) io_fault_.store(true);
  // Only now is the tail on the media: post it for read-back.
  if (readback_) readback_->CommitTail();
}

void McapWriter::Roll() {
  CloseCurrentPart();
  if (io_fault_.load()) return;
  ++part_index_;
  OpenPart();
}

void McapWriter::Write(const Channel& channel, const Message& msg) {
  Write(channel, RecordedMessage{msg.timestamp, msg.seq, msg.keyframe,
                                 msg.payload.str()});
}

void McapWriter::Write(const Channel& channel, const RecordedMessage& msg) {
  if (closed_ || io_fault_.load()) return;
  if (rotation_policy_) WriteContinuation(channel, msg);
  else WriteRecord(channel, msg);
}

void McapWriter::WriteRecord(const Channel& channel, const RecordedMessage& msg) {
  if (closed_ || io_fault_.load()) return;

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
    } else if (max_bytes_ > 0 && part_bytes_ >= max_bytes_ + max_bytes_ / 8) {
      Roll();
    }
  } else if (!rotation_policy_ && rotating_ && ShouldRoll()) {
    Roll();  // opt-in off: original byte-exact roll
  }
  if (io_fault_.load()) return;

  // Initial decoder warm-up: legacy callers repeat it per part; a continuation
  // chain repeats it only for each local camera's first-ever IDR. Continuation
  // P-frames reference the previous part and must be retained verbatim.
  if (is_video && (!rotation_policy_ || LocalVideo(channel)) &&
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
  if (io_fault_.load(std::memory_order_relaxed)) return;
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
  FlushStaged(false, false);
  closed_ = true;
  if (writer_) CloseCurrentPart();
  if (io_fault_.load(std::memory_order_relaxed))
    bytes_written_.store(std::min(bytes_written_.load(), physical_closed_));
  // Bounded by close_flush_ms (0 = none); whatever is left is counted
  // skipped and the ring is released.
  if (readback_) readback_->Finish();
}


bool McapWriter::LocalVideo(const Channel& channel) const {
  if (!rotation_policy_ || channel.schema_name != kCompressedVideoSchema) return false;
  const auto& ids = rotation_policy_->local_video_ids;
  return std::find(ids.begin(), ids.end(), channel.id) != ids.end();
}

bool McapWriter::ForcedCutDue() const {
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
  return io_fault_.load(std::memory_order_relaxed) ||
         (readback_ && readback_->storage_fault());
}

}  // namespace visio_schema::mcap
