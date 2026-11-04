// part_file — CloexecFileWriter, the IWritable behind each part's file.
// Private to src/mcap; included by writer/parts.cc alone, which is why its
// helpers keep the unnamed namespace they had in writer.cc.
#pragma once

#include <fcntl.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <utility>

#include "visio_schema/log.hpp"
#include "visio_schema/mcap/recording_crypto.hpp"
#include "visio_schema/mcap/storage.hpp"

#include "mcap/file_sync.hpp"
#include "mcap/part_outcome.hpp"
#include "mcap/span_readback.hpp"
#include "mcap/writer/vendored_mcap.hpp"

namespace visio_schema::mcap {

namespace {

// The part file's stdio buffer. Small on purpose, and the cipher staging
// buffer below must stay STRICTLY larger than it — both reasons are in
// CloexecFileWriter::open() and ::handleWrite(); the invariant is asserted
// under the class.
constexpr std::size_t kStdioBufferBytes = 16 * 1024;
constexpr std::size_t kCipherScratchBytes = 64 * 1024;

// What one attempt to open a part came to, at the writable.
struct PartOpening {
  IoResult failure;      // none when the part is open
  bool created = false;  // a file was made for it: removed if it failed
};

// MCAP framing and VREC encryption use the same checked device backend.
// Every failed step is reported to the part's outcome (part_outcome.hpp),
// which decides whether it costs the part or ends the recording.
class CloexecFileWriter final : public ::mcap::IWritable {
 public:
  explicit CloexecFileWriter(std::uint64_t sync_span_bytes,
                             const RecordingKey* key, SpanReadback* readback,
                             PartOutcome* outcome,
                             std::shared_ptr<WriterStorage> storage,
                             std::chrono::milliseconds checkpoint_interval)
      : outcome_(outcome), storage_(std::move(storage)),
        checkpoint_interval_(checkpoint_interval), encrypting_(key != nullptr),
        readback_(readback), sync_span_bytes_(sync_span_bytes ?
          std::max<std::uint64_t>(sync_span_bytes, file_sync::kPageBytes) : 0) {
    if (key) key_ = *key;
  }
  ~CloexecFileWriter() override { end(); }

  // Make `filename` the open part: created, and for an encrypted recording
  // its VREC header written. A failed attempt says its step and errno; a
  // file it made is closed (Discard) for the owner to remove, holding no
  // recorded data.
  PartOpening open(const std::string& filename) {
    PartOpening opening;
    file_ = storage_->Create(filename, opening.failure);
    if (!file_) {
      opening.failure = outcome_->Explained(opening.failure);
      return opening;
    }
    opening.created = true;
    opening.failure = {};
    checkpoint_at_ = std::chrono::steady_clock::now();
    if (encrypting_) opening.failure = BeginVrec(filename);
    if (opening.failure.error) Discard();
    return opening;
  }

  void handleWrite(const std::byte* data, uint64_t size) override {
    if (!file_ || outcome_->refused()) return;
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
        // Nothing of this slice landed: the part has lost bytes.
        outcome_->Note({IoStep::append, EIO, 0, true},
                       "VREC encrypt failed at offset " +
                           std::to_string(size_));
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
    // A short write is an SD/ENOSPC failure stdio may otherwise defer to
    // fclose; a silently short part would still carry plausible offsets.
    // Advance by what actually landed so every downstream offset (sync
    // spans included) matches the file; the part is kept, unconfirmed.
    if (!result || wrote != size) outcome_->Note(result);
    // The read-back's copy of what just landed, keyed by FILE offset —
    // below the cipher, so it is the bytes as they are on disk. This
    // memcpy is the read-back's entire cost on the write path.
    if (readback_) readback_->OnBytesWritten(file_size(), data, wrote);
    size_ += wrote;
    if (sync_span_bytes_ > 0 && file_size() - synced_off_ >= sync_span_bytes_)
      SyncSpan();
    // Periodic, and never fatal by itself: a failed one costs the part.
    if (checkpoint_interval_.count() > 0 &&
        std::chrono::steady_clock::now() - checkpoint_at_ >= checkpoint_interval_) {
      Report(file_->Checkpoint());
      checkpoint_at_ = std::chrono::steady_clock::now();
    }
    return wrote;
  }

  void end() override {
    if (file_) {
      const uint64_t final_size = file_size();
      Report(file_->Checkpoint());
      const auto closed = file_->Close();
      Report(closed);
      file_.reset();
      outcome_->Closed(closed.accepted);
      // Everything past the last evicted span — the span synced but not
      // yet waited on, plus the partial tail — is on disk now but not
      // verified; the owner posts it once the part is fsynced.
      if (readback_)
        readback_->NotePartEnd(evicted_end_, final_size, sync_disabled_);
    }
    ResetPart();
  }

  uint64_t size() const override { return size_; }

 private:
  // A part that never opened, closed with nothing reported: the owner
  // removes it, and nothing of it belongs to the recording.
  void Discard() {
    if (file_) file_->Close();  // moot: the file is removed
    file_.reset();
    ResetPart();
  }
  void ResetPart() {
    cipher_.reset();
    scratch_.reset();  // nothing between parts holds a part's staging buffer
    header_bytes_ = 0;
    size_ = 0;
    synced_off_ = 0;
    prev_off_ = 0;
    prev_len_ = 0;
    evicted_end_ = 0;
    sync_disabled_ = false;
  }
  void Report(const IoResult& result) {
    if (!result) outcome_->Note(result);
  }
  PartOutcome* const outcome_;
  const std::shared_ptr<WriterStorage> storage_;
  const std::chrono::milliseconds checkpoint_interval_;
  std::chrono::steady_clock::time_point checkpoint_at_;

  // File offset = plaintext offset + any VREC header. SyncSpan and fadvise
  // address the FILE; MCAP's index addresses the plaintext. They differ by
  // exactly 32 bytes on an encrypted part, and conflating the two would skew
  // every writeback span by that much.
  uint64_t file_size() const { return size_ + header_bytes_; }

  // Mint a nonce, write the 32-byte plaintext header, arm the cipher. Called
  // from open() before any MCAP byte reaches the file. The failure, if any.
  IoResult BeginVrec(const std::string& filename) {
    RecordingNonce nonce{};
    if (!RandomNonce(&nonce)) {
      // Never fall back to a fixed nonce: two parts sharing key+nonce share a
      // keystream, and XORing them recovers both plaintexts with no key.
      log::Write(log::Severity::kError,
                 "mcap: %s: no CSPRNG for a part nonce, refusing to encrypt",
                 filename.c_str());
      return {IoStep::open, EIO, 0, true};
    }
    VrecHeader header;
    header.key_fp = RecordingKeyFingerprint(key_);
    header.nonce = nonce;
    std::array<std::uint8_t, kVrecHeaderBytes> raw{};
    WriteVrecHeader(header, raw.data());
    // A part that cannot take its header is a part that cannot be opened
    // (McapWriter::OpenPart), not a part failure to note.
    const auto wrote = file_->Append(raw.data(), raw.size());
    if (!wrote || wrote.accepted != raw.size())
      return outcome_->Explained(wrote);
    // The header is on-disk bytes like any other: the read-back compares
    // the file from offset 0.
    if (readback_) readback_->OnBytesWritten(0, raw.data(), raw.size());
    header_bytes_ = raw.size();
    auto cipher = std::make_unique<RecordingCipher>(key_, nonce);
    if (!cipher->valid()) {
      log::Write(log::Severity::kError, "mcap: %s: VREC cipher init failed",
                 filename.c_str());
      return {IoStep::open, EIO, 0, true};
    }
    // Staged before the cipher is armed, so `cipher_ != nullptr` (what
    // handleWrite branches on) can never outrun the buffer it writes into.
    scratch_ = std::make_unique<std::uint8_t[]>(kCipherScratchBytes);
    cipher_ = std::move(cipher);
    return {};
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
  // (file_sync::WritebackAndEvict explains the flag triple). Writeback the
  // storage does not offer stops the bounding for the rest of the part, and
  // costs nothing else. Any other failure may have lost the span (a later
  // fsync succeeds over pages marked clean): the part is cut, and bounding
  // goes on until it is.
  void SyncSpan() {
    // 4 KiB on every kernel we ship (RV1106/RV1126B); a larger-page target
    // would only strand a few clean pages per span, not corrupt anything.
    const auto flushed = file_->Flush();
    if (!flushed) {
      outcome_->Note(flushed);  // lossy: the part is cut at the next record
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
    int err = file_->Writeback(off, len, false);
    if (err == 0 && prev_len_ > 0) err = EvictPreviousSpan();
    if (err != 0) WritebackFailed(err);
    prev_off_ = off;
    prev_len_ = len;
    synced_off_ = end;
#else
    synced_off_ = file_sync::RoundDownToPage(file_size());
#endif
  }

  // A span's writeback failed with `err`: see SyncSpan for what it costs.
  void WritebackFailed(int err) {
    if (!file_sync::WritebackUnsupported(err)) {
      outcome_->Note({IoStep::writeback, err, 0, true});
      return;
    }
    if (!sync_disabled_)
      log::Write(log::Severity::kWarning,
                 "mcap: span writeback unsupported (%s): dirty-set bounding "
                 "disabled for this part",
                 std::strerror(err));
    StopBounding();
  }

  void StopBounding() {
    if (sync_disabled_) return;
    sync_disabled_ = true;
    if (readback_) readback_->OnSyncDisabled();
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

}  // namespace

}  // namespace visio_schema::mcap
