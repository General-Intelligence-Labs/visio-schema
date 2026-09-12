// SpanReadback — write-time verification of what the storage medium holds;
// the mechanism behind readback.hpp.
//
// A recorded part came back with stale sector data mixed into it: the
// writer wrote correct bytes and the SD card silently returned old sectors
// afterwards. So every byte the sink writes is also copied into a ring;
// when SyncSpan has written a span back and evicted it from the page cache,
// the span is queued; a caller-driven Step reads it from the medium
// (O_DIRECT) and compares it with the ring, rewriting the whole span once
// on a mismatch and re-reading it.
//
// Threads. Exactly two touch this object. The WRITER (the sink's own
// thread) calls BeginPart / OnBytesWritten / OnSpanEvicted /
// OnSyncDisabled / NotePartEnd / CommitTail and, last, Finish. ONE
// stepping thread of the caller's choosing calls Step. No thread is
// created here. The writer side never blocks — the ring copy is one
// memcpy, the span queue is a fixed SPSC ring that drops (counting
// spans_skipped) when full, and nothing on that side takes a lock. Step
// and Finish serialize on one mutex, so a Close racing a step is safe.
// Stats are atomics, readable from anywhere. Private to src/mcap.
//
// The compare reads ring bytes the writer may be overwriting: that is a
// formal data race, made sound by the reserve-then-copy protocol below
// (SpanStillInRing) — a compare the writer tore is discarded, never
// trusted either way.
#pragma once

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>
#include <string>

#include "part_readback_file.hpp"
#include "visio_schema/mcap/readback.hpp"

namespace visio_schema::mcap {

class SpanReadback {
 public:
  explicit SpanReadback(McapReadbackOptions opts);
  ~SpanReadback();
  SpanReadback(const SpanReadback&) = delete;
  SpanReadback& operator=(const SpanReadback&) = delete;

  // ---- writer side ---------------------------------------------------
  // A part file is about to open at `path`; its file offset 0 maps to the
  // next page-aligned ring position, so page-aligned file offsets stay
  // page-aligned in the ring and a wrap never splits a page.
  void BeginPart(const std::string& path);
  // `n` bytes landed at file offset `file_off` — below the cipher, so
  // exactly the bytes on disk. One memcpy; publishes the ring head.
  void OnBytesWritten(std::uint64_t file_off, const void* data,
                      std::size_t n);
  // [file_off, +len) of the current part is written back and evicted.
  void OnSpanEvicted(std::uint64_t file_off, std::uint64_t len);
  // The sink's span writeback latched off for this part: nothing further
  // in it will be evicted, so the rest of the part is counted skipped.
  void OnSyncDisabled();
  // The part's stream ended. [unverified_from, file_size) — the last
  // synced-but-not-evicted span plus the partial tail — is remembered, not
  // queued: the part still needs its fsync first.
  void NotePartEnd(std::uint64_t unverified_from, std::uint64_t file_size,
                   bool sync_disabled);
  // After the part's fsync: evict the whole file and queue the tail.
  void CommitTail();
  // Last writer-side call. Waives the settling wait (no more bytes will
  // come), runs steps inline for at most close_flush_ms, counts what is
  // left as skipped, logs the recording's totals, and frees the ring —
  // nothing stays resident.
  void Finish();

  // ---- stepping side -------------------------------------------------
  // Verify at most one span, resuming one a previous budget left
  // unfinished; at least one piece per call when a span is ready. True if
  // any work was done.
  bool Step(std::chrono::milliseconds budget);

  // ---- any thread ----------------------------------------------------
  std::size_t pending() const;
  McapReadbackStats stats() const;
  bool storage_fault() const {
    return storage_fault_.load(std::memory_order_relaxed);
  }

 private:
  // A queued span. `ring_pos` is its position in the ring's byte stream
  // (monotonic across parts); `tail` marks a part's closing span, whose
  // length is not a page multiple.
  struct Span {
    const std::string* path;
    std::uint64_t file_off;
    std::uint64_t len;
    std::uint64_t ring_pos;
    bool tail;
  };
  enum class Pass { kVerify, kRewrite, kReverify };
  struct InFlight {
    Span span;
    Pass pass;
    std::uint64_t done;  // bytes completed in the current pass
  };
  enum class PieceOutcome { kSpanContinues, kSpanDone };
  // Where [pos, pos+n) of the ring stream sits in the buffer: first_len
  // bytes at first_idx, the rest (if any) from index 0.
  struct RingSlice {
    std::size_t first_idx;
    std::size_t first_len;
  };
  // Where the first differing byte was, and what was there.
  struct Diff {
    std::uint64_t file_off;
    std::uint8_t expected[8];
    std::uint8_t got[8];
    std::size_t shown;
  };

  static constexpr std::size_t kQueueSlots = 64;

  // writer side
  void Enqueue(const Span& s);
  void CountSkipped(std::uint64_t n = 1);
  void ForgetPartsNoSpanNames();

  // stepping side (under step_mu_)
  bool StepLocked(std::chrono::milliseconds budget);
  bool StartNextSpan();
  PieceOutcome RunPiece();
  PieceOutcome VerifyPiece(std::uint64_t off, std::uint64_t pos,
                           std::size_t n);
  PieceOutcome RewritePiece(std::uint64_t off, std::uint64_t pos,
                            std::size_t n);
  // The in-flight span could not be finished: a read failure, the ring
  // moving on, the recording closing. Past the first pass the medium is
  // KNOWN wrong, so that is a storage fault; before it, a skip.
  PieceOutcome AbandonInFlight(const char* why);
  PieceOutcome NoteReadFailed(PartReadbackFile::ReadOutcome outcome);
  void FinishSpan();
  void FireSeam(const Span& s, McapReadbackPass pass) const;
  bool SpanStillInRing(const Span& s) const;
  RingSlice SliceAtWrap(std::uint64_t pos, std::size_t n) const;
  void CopyFromRing(std::uint64_t pos, void* dst, std::size_t n) const;
  bool FindFirstDiffAgainstRing(std::uint64_t pos, std::uint64_t file_off,
                                const std::uint8_t* got, std::size_t n,
                                Diff* diff) const;
  void LogMismatch(const Span& s, const Diff& d) const;
  void NoteUnrepaired(const Span& s, const char* why);
  void LogTotals() const;
  void ReleaseBuffers();

  const McapReadbackOptions opts_;
  const std::uint64_t ring_bytes_;   // page multiple
  const std::size_t piece_bytes_;    // page multiple
  std::uint8_t* ring_ = nullptr;     // page-aligned; null = off
  std::uint8_t* scratch_ = nullptr;  // one piece, page-aligned (O_DIRECT)

  // Ring stream positions. `head_` is the next byte to be written and is
  // published AFTER the copy; `reserve_` is the same value published
  // BEFORE it, so a step can tell, after comparing, whether the slots it
  // read might have been torn under it (Dekker, seq_cst fences).
  std::atomic<std::uint64_t> head_{0};
  std::atomic<std::uint64_t> reserve_{0};

  // Writer-side only. Part paths live in a pointer-stable deque; a path
  // is dropped once no queued span names it (ForgetPartsNoSpanNames).
  std::deque<std::string> part_paths_;
  std::string first_part_path_;  // for the totals line
  const std::string* cur_part_ = nullptr;
  std::uint64_t part_base_ = 0;  // ring position of the part's offset 0
  std::optional<Span> pending_tail_;

  // The SPSC span queue: the writer pushes, the stepper pops. The span in
  // flight stays at the pop index until it is finished.
  Span queue_[kQueueSlots] = {};
  std::atomic<std::uint32_t> push_{0};
  std::atomic<std::uint32_t> pop_{0};
  std::atomic<bool> no_more_writes_{false};

  // Stepping-side only.
  mutable std::mutex step_mu_;
  PartReadbackFile file_;
  std::optional<InFlight> in_flight_;

  std::atomic<std::uint64_t> spans_verified_{0};
  std::atomic<std::uint64_t> bytes_verified_{0};
  std::atomic<std::uint64_t> spans_mismatched_{0};
  std::atomic<std::uint64_t> spans_rewritten_ok_{0};
  std::atomic<std::uint64_t> spans_unrepaired_{0};
  std::atomic<std::uint64_t> spans_skipped_{0};
  std::atomic<std::uint64_t> read_failed_{0};
  std::atomic<std::uint64_t> max_lag_bytes_{0};
  std::atomic<bool> storage_fault_{false};
};

}  // namespace visio_schema::mcap
