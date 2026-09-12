#include "span_readback.hpp"

#include <fcntl.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <utility>

#include "file_sync.hpp"
#include "visio_schema/mcap/recording_crypto.hpp"  // HexOf

namespace visio_schema::mcap {

namespace {

// The cluster the field failure was aligned to; the log reports the offset
// within it so a recurrence is recognisable at a glance.
constexpr std::uint64_t kClusterBytes = 128 * 1024;

using Clock = std::chrono::steady_clock;

std::uint8_t* AllocPageAligned(std::size_t bytes) {
  void* p = nullptr;
  if (::posix_memalign(&p, file_sync::kPageBytes, bytes) != 0) return nullptr;
  return static_cast<std::uint8_t*>(p);
}

unsigned long long ull(std::uint64_t v) {
  return static_cast<unsigned long long>(v);
}

}  // namespace

SpanReadback::SpanReadback(McapReadbackOptions opts)
    : opts_(std::move(opts)),
      ring_bytes_(file_sync::RoundUpToPage(
          std::max<std::uint64_t>(opts_.ring_bytes, file_sync::kPageBytes))),
      piece_bytes_(static_cast<std::size_t>(file_sync::RoundUpToPage(
          std::max<std::uint64_t>(opts_.piece_bytes, file_sync::kPageBytes)))) {
#if !defined(__linux__)
  std::fprintf(stderr, "mcap readback: Linux only — off for this recording\n");
  return;
#endif
  ring_ = AllocPageAligned(static_cast<std::size_t>(ring_bytes_));
  scratch_ = AllocPageAligned(piece_bytes_);
  if (!ring_ || !scratch_) {
    ReleaseBuffers();
    std::fprintf(stderr,
                 "mcap readback: cannot allocate %llu + %zu bytes — "
                 "read-back off for this recording\n",
                 ull(ring_bytes_), piece_bytes_);
  }
}

SpanReadback::~SpanReadback() { ReleaseBuffers(); }

void SpanReadback::ReleaseBuffers() {
  std::free(ring_);
  std::free(scratch_);
  ring_ = nullptr;
  scratch_ = nullptr;
}

// ---- writer side ---------------------------------------------------------

void SpanReadback::BeginPart(const std::string& path) {
  if (pending_tail_) CountSkipped();  // a tail never committed
  pending_tail_.reset();
  ForgetPartsNoSpanNames();
  part_paths_.push_back(path);
  if (first_part_path_.empty()) first_part_path_ = path;
  cur_part_ = &part_paths_.back();
  part_base_ =
      file_sync::RoundUpToPage(head_.load(std::memory_order_relaxed));
  reserve_.store(part_base_, std::memory_order_relaxed);
  head_.store(part_base_, std::memory_order_release);
}

// Spans are queued in file order and the span in flight stays at the pop
// index, so the oldest path still referenced is the one at the queue's
// front; every earlier part's path can go. The slot's `path` is written
// by this thread only, so reading it here races nothing.
void SpanReadback::ForgetPartsNoSpanNames() {
  const std::uint32_t pop = pop_.load(std::memory_order_acquire);
  const std::string* oldest = push_.load(std::memory_order_relaxed) == pop
                                  ? nullptr
                                  : queue_[pop % kQueueSlots].path;
  while (part_paths_.size() > 1 && &part_paths_.front() != oldest &&
         &part_paths_.front() != cur_part_)
    part_paths_.pop_front();
}

void SpanReadback::OnBytesWritten(std::uint64_t file_off, const void* data,
                                  std::size_t n) {
  if (!ring_ || n == 0) return;
  auto* src = static_cast<const std::uint8_t*>(data);
  std::uint64_t pos = part_base_ + file_off;
  if (n > ring_bytes_) {
    // Only the newest ring's worth of a huge write can ever be compared.
    const std::size_t drop = n - static_cast<std::size_t>(ring_bytes_);
    src += drop;
    pos += drop;
    n -= drop;
  }
  const std::uint64_t end = pos + n;
  // Reserve BEFORE touching the ring. A step that compared these slots
  // re-checks the reservation after its compare (SpanStillInRing), so it
  // can tell a compare torn by this copy from a real mismatch.
  reserve_.store(end, std::memory_order_relaxed);
  std::atomic_thread_fence(std::memory_order_seq_cst);
  const RingSlice at = SliceAtWrap(pos, n);
  std::memcpy(ring_ + at.first_idx, src, at.first_len);
  if (at.first_len < n)
    std::memcpy(ring_, src + at.first_len, n - at.first_len);
  head_.store(end, std::memory_order_release);
}

void SpanReadback::OnSpanEvicted(std::uint64_t file_off, std::uint64_t len) {
  Enqueue(Span{cur_part_, file_off, len, part_base_ + file_off, false});
}

void SpanReadback::OnSyncDisabled() { CountSkipped(); }

void SpanReadback::NotePartEnd(std::uint64_t unverified_from,
                               std::uint64_t file_size, bool sync_disabled) {
  if (file_size <= unverified_from) return;
  if (sync_disabled) {
    CountSkipped();
    return;
  }
  pending_tail_ = Span{cur_part_, unverified_from, file_size - unverified_from,
                       part_base_ + unverified_from, true};
}

void SpanReadback::CommitTail() {
  if (!pending_tail_) return;
  // The sink's fd is closed; open one just to evict the whole part, so the
  // tail read is served by the medium like every other span.
  const int fd = ::open(pending_tail_->path->c_str(), O_RDONLY | O_CLOEXEC);
  if (fd >= 0) {
    file_sync::EvictPages(fd, 0, 0);
    ::close(fd);
  }
  Enqueue(*pending_tail_);
  pending_tail_.reset();
}

void SpanReadback::Enqueue(const Span& s) {
  if (!ring_) return;
  if (s.len > ring_bytes_) {
    CountSkipped();
    return;
  }
  const std::uint32_t push = push_.load(std::memory_order_relaxed);
  if (push - pop_.load(std::memory_order_acquire) == kQueueSlots) {
    CountSkipped();  // the stepper is behind; the writer never waits for it
    return;
  }
  queue_[push % kQueueSlots] = s;
  push_.store(push + 1, std::memory_order_release);
}

void SpanReadback::CountSkipped(std::uint64_t n) {
  spans_skipped_.fetch_add(n, std::memory_order_relaxed);
}

void SpanReadback::Finish() {
  std::lock_guard<std::mutex> lk(step_mu_);
  no_more_writes_.store(true, std::memory_order_release);
  if (ring_ && opts_.close_flush_ms > 0) {
    const auto deadline =
        Clock::now() + std::chrono::milliseconds(opts_.close_flush_ms);
    for (;;) {
      const auto now = Clock::now();
      if (now >= deadline) break;
      const auto left =
          std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
      if (!StepLocked(left)) break;
    }
  }
  // Whatever is left was never verified: the span in flight keeps its
  // verdict (a mismatch abandoned mid-rewrite is a fault), the rest is
  // counted skipped. Then let go of the RAM — nothing stays resident
  // between recordings.
  if (in_flight_) {
    AbandonInFlight("recording closed before the span was re-read");
    FinishSpan();
  }
  const std::uint32_t push = push_.load(std::memory_order_relaxed);
  CountSkipped(push - pop_.load(std::memory_order_relaxed));
  pop_.store(push, std::memory_order_release);
  if (pending_tail_) CountSkipped();
  pending_tail_.reset();
  if (ring_) LogTotals();
  file_.Close();
  ReleaseBuffers();
}

void SpanReadback::LogTotals() const {
  const McapReadbackStats s = stats();
  std::fprintf(stderr,
               "mcap readback: %s: verified %llu spans (%llu B), mismatched "
               "%llu, rewritten %llu, unrepaired %llu, skipped %llu, read "
               "failures %llu, max lag %llu B\n",
               first_part_path_.c_str(), ull(s.spans_verified),
               ull(s.bytes_verified), ull(s.spans_mismatched),
               ull(s.spans_rewritten_ok), ull(s.spans_unrepaired),
               ull(s.spans_skipped), ull(s.read_failed), ull(s.max_lag_bytes));
}

// ---- stepping side -------------------------------------------------------

bool SpanReadback::Step(std::chrono::milliseconds budget) {
  std::lock_guard<std::mutex> lk(step_mu_);
  return StepLocked(budget);
}

bool SpanReadback::StepLocked(std::chrono::milliseconds budget) {
  if (!ring_) return false;
  if (!in_flight_ && !StartNextSpan()) return false;
  const auto t0 = Clock::now();
  for (;;) {
    if (RunPiece() == PieceOutcome::kSpanDone) {
      FinishSpan();
      return true;
    }
    if (Clock::now() - t0 >= budget) return true;
  }
}

// The next queued span that is settled and still resident. One the writer
// has already overrun is skipped without a read: its compare could prove
// nothing.
bool SpanReadback::StartNextSpan() {
  for (;;) {
    const std::uint32_t pop = pop_.load(std::memory_order_relaxed);
    if (push_.load(std::memory_order_acquire) == pop) return false;
    const Span& s = queue_[pop % kQueueSlots];
    const std::uint64_t head = head_.load(std::memory_order_acquire);
    if (head - s.ring_pos > ring_bytes_) {
      CountSkipped();
      pop_.store(pop + 1, std::memory_order_release);
      continue;
    }
    const std::uint64_t lag = head - (s.ring_pos + s.len);
    if (!no_more_writes_.load(std::memory_order_acquire) &&
        lag < opts_.settle_bytes)
      return false;
    if (lag > max_lag_bytes_.load(std::memory_order_relaxed))
      max_lag_bytes_.store(lag, std::memory_order_relaxed);
    in_flight_ = InFlight{s, Pass::kVerify, 0};
    FireSeam(s, McapReadbackPass::kFirstRead);
    return true;
  }
}

void SpanReadback::FireSeam(const Span& s, McapReadbackPass pass) const {
  if (opts_.before_span_read_for_test)
    opts_.before_span_read_for_test(*s.path, s.file_off, s.len, pass);
}

SpanReadback::PieceOutcome SpanReadback::RunPiece() {
  InFlight& c = *in_flight_;
  const Span& s = c.span;
  if (!file_.Open(*s.path)) return NoteReadFailed(
      PartReadbackFile::ReadOutcome::kError);
  const auto n = static_cast<std::size_t>(
      std::min<std::uint64_t>(piece_bytes_, s.len - c.done));
  const std::uint64_t off = s.file_off + c.done;
  const std::uint64_t pos = s.ring_pos + c.done;
  return c.pass == Pass::kRewrite ? RewritePiece(off, pos, n)
                                  : VerifyPiece(off, pos, n);
}

SpanReadback::PieceOutcome SpanReadback::VerifyPiece(std::uint64_t off,
                                                     std::uint64_t pos,
                                                     std::size_t n) {
  InFlight& c = *in_flight_;
  const Span& s = c.span;
  const auto read = file_.ReadPiece(off, scratch_, n);
  if (read != PartReadbackFile::ReadOutcome::kOk) return NoteReadFailed(read);
  Diff d{};
  const bool differs = FindFirstDiffAgainstRing(pos, off, scratch_, n, &d);
  // A compare is trustworthy only once the ring is known not to have
  // moved under it.
  if (!SpanStillInRing(s))
    return AbandonInFlight("the ring moved on during the read-back");
  if (differs) {
    if (c.pass == Pass::kVerify) {
      spans_mismatched_.fetch_add(1, std::memory_order_relaxed);
      LogMismatch(s, d);
      c.pass = Pass::kRewrite;
      c.done = 0;
      return PieceOutcome::kSpanContinues;
    }
    NoteUnrepaired(s, "still wrong after the rewrite");
    return PieceOutcome::kSpanDone;
  }
  c.done += n;
  if (c.done < s.len) return PieceOutcome::kSpanContinues;
  spans_verified_.fetch_add(1, std::memory_order_relaxed);
  bytes_verified_.fetch_add(s.len, std::memory_order_relaxed);
  if (c.pass == Pass::kReverify) {
    spans_rewritten_ok_.fetch_add(1, std::memory_order_relaxed);
    std::fprintf(stderr,
                 "mcap readback: %s [%llu,+%llu) rewritten and verified\n",
                 s.path->c_str(), ull(s.file_off), ull(s.len));
  }
  return PieceOutcome::kSpanDone;
}

SpanReadback::PieceOutcome SpanReadback::RewritePiece(std::uint64_t off,
                                                      std::uint64_t pos,
                                                      std::size_t n) {
  InFlight& c = *in_flight_;
  const Span& s = c.span;
  // Never write from the ring itself: copy out, then prove the copy was
  // not torn by the writer before a byte of it reaches the file.
  CopyFromRing(pos, scratch_, n);
  if (!SpanStillInRing(s))
    return AbandonInFlight("the ring moved on during the rewrite");
  if (!file_.WritePiece(off, scratch_, n)) {
    NoteUnrepaired(s, std::strerror(file_.last_errno()));
    return PieceOutcome::kSpanDone;
  }
  c.done += n;
  if (c.done < s.len) return PieceOutcome::kSpanContinues;
  c.pass = Pass::kReverify;
  c.done = 0;
  FireSeam(s, McapReadbackPass::kReadAfterRewrite);
  return PieceOutcome::kSpanContinues;
}

SpanReadback::PieceOutcome SpanReadback::AbandonInFlight(const char* why) {
  const InFlight& c = *in_flight_;
  if (c.pass != Pass::kVerify) {
    NoteUnrepaired(c.span, why);
  } else {
    CountSkipped();
  }
  return PieceOutcome::kSpanDone;
}

// A read the medium could not serve. EIO on bytes the medium accepted is
// the strongest sign of a bad card there is, so it is a storage fault; a
// short file (the part is not what it was) is counted and logged.
SpanReadback::PieceOutcome SpanReadback::NoteReadFailed(
    PartReadbackFile::ReadOutcome outcome) {
  const InFlight& c = *in_flight_;
  const Span& s = c.span;
  const bool error = outcome == PartReadbackFile::ReadOutcome::kError;
  const char* why = error ? std::strerror(file_.last_errno()) : "short file";
  if (c.pass != Pass::kVerify || (error && file_.last_errno() == EIO)) {
    NoteUnrepaired(s, why);
    return PieceOutcome::kSpanDone;
  }
  read_failed_.fetch_add(1, std::memory_order_relaxed);
  std::fprintf(stderr, "mcap readback: %s [%llu,+%llu) read failed: %s\n",
               s.path->c_str(), ull(s.file_off), ull(s.len), why);
  return PieceOutcome::kSpanDone;
}

void SpanReadback::FinishSpan() {
  in_flight_.reset();
  pop_.store(pop_.load(std::memory_order_relaxed) + 1,
             std::memory_order_release);
}

bool SpanReadback::SpanStillInRing(const Span& s) const {
  // Dekker against OnBytesWritten's reserve-then-copy: with seq_cst fences
  // on both sides, a reservation this load does not see cannot have
  // overwritten anything the caller read before the fence.
  std::atomic_thread_fence(std::memory_order_seq_cst);
  return reserve_.load(std::memory_order_relaxed) - s.ring_pos <= ring_bytes_;
}

SpanReadback::RingSlice SpanReadback::SliceAtWrap(std::uint64_t pos,
                                                  std::size_t n) const {
  const auto idx = static_cast<std::size_t>(pos % ring_bytes_);
  return RingSlice{idx,
                   std::min(n, static_cast<std::size_t>(ring_bytes_) - idx)};
}

void SpanReadback::CopyFromRing(std::uint64_t pos, void* dst,
                                std::size_t n) const {
  const RingSlice at = SliceAtWrap(pos, n);
  std::memcpy(dst, ring_ + at.first_idx, at.first_len);
  if (at.first_len < n) {
    std::memcpy(static_cast<std::uint8_t*>(dst) + at.first_len, ring_,
                n - at.first_len);
  }
}

bool SpanReadback::FindFirstDiffAgainstRing(std::uint64_t pos,
                                            std::uint64_t file_off,
                                            const std::uint8_t* got,
                                            std::size_t n, Diff* diff) const {
  const RingSlice at = SliceAtWrap(pos, n);
  std::size_t i = static_cast<std::size_t>(
      std::mismatch(got, got + at.first_len, ring_ + at.first_idx).first -
      got);
  if (i == at.first_len && at.first_len < n) {
    i = at.first_len +
        static_cast<std::size_t>(
            std::mismatch(got + at.first_len, got + n, ring_).first -
            (got + at.first_len));
  }
  if (i >= n) return false;
  diff->file_off = file_off + i;
  diff->shown = std::min<std::size_t>(sizeof diff->expected, n - i);
  CopyFromRing(pos + i, diff->expected, diff->shown);
  std::memcpy(diff->got, got + i, diff->shown);
  return true;
}

void SpanReadback::LogMismatch(const Span& s, const Diff& d) const {
  std::fprintf(stderr,
               "mcap readback: %s %s [%llu,+%llu) mismatch at %llu "
               "(cluster+%llu): expected %s got %s — rewriting the span\n",
               s.path->c_str(), s.tail ? "tail" : "span", ull(s.file_off),
               ull(s.len), ull(d.file_off), ull(d.file_off % kClusterBytes),
               HexOf(d.expected, d.shown).c_str(),
               HexOf(d.got, d.shown).c_str());
}

void SpanReadback::NoteUnrepaired(const Span& s, const char* why) {
  spans_unrepaired_.fetch_add(1, std::memory_order_relaxed);
  std::fprintf(stderr, "mcap readback: %s [%llu,+%llu) %s — leaving it\n",
               s.path->c_str(), ull(s.file_off), ull(s.len), why);
  if (!storage_fault_.exchange(true, std::memory_order_relaxed)) {
    std::fprintf(stderr,
                 "mcap readback: storage fault on %s — the medium does not "
                 "hold what was written; recording continues\n",
                 s.path->c_str());
  }
}

// ---- any thread ----------------------------------------------------------

std::size_t SpanReadback::pending() const {
  return push_.load(std::memory_order_relaxed) -
         pop_.load(std::memory_order_relaxed);
}

McapReadbackStats SpanReadback::stats() const {
  McapReadbackStats s;
  s.spans_verified = spans_verified_.load(std::memory_order_relaxed);
  s.bytes_verified = bytes_verified_.load(std::memory_order_relaxed);
  s.spans_mismatched = spans_mismatched_.load(std::memory_order_relaxed);
  s.spans_rewritten_ok = spans_rewritten_ok_.load(std::memory_order_relaxed);
  s.spans_unrepaired = spans_unrepaired_.load(std::memory_order_relaxed);
  s.spans_skipped = spans_skipped_.load(std::memory_order_relaxed);
  s.read_failed = read_failed_.load(std::memory_order_relaxed);
  s.max_lag_bytes = max_lag_bytes_.load(std::memory_order_relaxed);
  return s;
}

}  // namespace visio_schema::mcap
