#include "visio_schema/mcap/payload_arena.hpp"

#include <sys/mman.h>

#include <cassert>
#include <cerrno>
#include <cstring>

#include "visio_schema/log.hpp"

namespace visio_schema::mcap {

PayloadArena::Record& PayloadArena::Record::operator=(Record&& o) noexcept {
  if (this != &o) {
    Reset();
    arena_ = o.arena_;
    offset_ = o.offset_;
    size_ = o.size_;
    o.arena_ = nullptr;
  }
  return *this;
}

PayloadArena::Record::~Record() { Reset(); }

void PayloadArena::Record::Reset() noexcept {
  auto* arena = arena_;
  if (auto reclaim = Release()) {
    const int error = reclaim->Run();
    arena->CompleteReclaim(std::move(*reclaim), error);
  }
}

std::optional<PayloadArena::Reclaim> PayloadArena::Record::Release() noexcept {
  auto* arena = std::exchange(arena_, nullptr);
  return arena ? arena->Free(offset_) : std::nullopt;
}

std::string_view PayloadArena::Record::bytes() const {
  return {arena_->base_ + offset_, size_};
}

PayloadArena::PayloadArena(std::size_t capacity, std::size_t chunk_bytes,
                           std::size_t resident_chunks)
    : chunk_bytes_(chunk_bytes), resident_chunks_(resident_chunks) {
  const std::size_t chunks = chunk_bytes ? capacity / chunk_bytes : 0;
  if (chunks == 0) return;
  // NORESERVE: address space only. A page costs RAM from its first write
  // until its chunk is handed back.
  void* p = ::mmap(nullptr, chunks * chunk_bytes, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
  if (p == MAP_FAILED) {
    log::Write(log::Severity::kWarning,
               "PayloadArena: could not reserve %zu bytes (%s); queued frames "
               "stay in their producers' buffers",
               chunks * chunk_bytes, std::strerror(errno));
    return;
  }
  base_ = static_cast<char*>(p);
  live_.assign(chunks, 0);
  residency_.assign(chunks, Residency::Untouched);
}

PayloadArena::~PayloadArena() {
  if (base_) ::munmap(base_, capacity());
}

std::optional<PayloadArena::Record> PayloadArena::Put(std::string_view bytes) {
  const std::size_t len = bytes.size();
  const auto next = ChooseChunk(len);
  if (next == kNoChunk) return std::nullopt;
  if (next != current_) {
    current_ = next;
    fill_ = 0;
  }
  const std::size_t offset = current_ * chunk_bytes_ + fill_;
  std::memcpy(base_ + offset, bytes.data(), len);
  fill_ += len;
  ++live_[current_];
  residency_[current_] = Residency::Touched;
  return Record(this, offset, len);
}

bool PayloadArena::CanPut(std::size_t size) const {
  return ChooseChunk(size) != kNoChunk;
}

std::size_t PayloadArena::ChooseChunk(std::size_t size) const {
  if (!base_ || size == 0 || size > chunk_bytes_) return kNoChunk;
  if (current_ != kNoChunk && size <= chunk_bytes_ - fill_) return current_;
  return LowestFreeChunk();
}

std::optional<PayloadArena::Reclaim> PayloadArena::Free(std::size_t offset) {
  const std::size_t chunk = offset / chunk_bytes_;
  if (--live_[chunk] != 0) return std::nullopt;
  // Emptied, the current chunk fills again from its start, unless it lies
  // above the resident set: then it is given up, so a backlog that drains
  // with nothing arriving does not keep its last chunk.
  if (chunk == current_) {
    fill_ = 0;
    if (chunk < resident_chunks_) return std::nullopt;
    current_ = kNoChunk;
  }
  if (chunk < resident_chunks_) return std::nullopt;
  residency_[chunk] = Residency::Reclaiming;
  return Reclaim(this, chunk);
}

std::size_t PayloadArena::LowestFreeChunk() const {
  for (std::size_t c = 0; c < live_.size(); ++c)
    if (c != current_ && live_[c] == 0 &&
        residency_[c] != Residency::Reclaiming)
      return c;
  return kNoChunk;
}

int PayloadArena::Reclaim::Run() const noexcept {
  assert(arena_);
  if (::madvise(arena_->base_ + chunk_ * arena_->chunk_bytes_,
                arena_->chunk_bytes_, MADV_DONTNEED) == 0)
    return 0;
  const int error = errno;
  if (!arena_->hand_back_failed_logged_.exchange(true))
    log::Write(log::Severity::kWarning,
               "PayloadArena: madvise(DONTNEED) failed: %s — emptied chunks "
               "stay with the process",
               std::strerror(error));
  return error;
}

void PayloadArena::CompleteReclaim(Reclaim&& reclaim, int error) {
  assert(reclaim.arena_ == this);
  assert(residency_[reclaim.chunk_] == Residency::Reclaiming);
  residency_[reclaim.chunk_] =
      error ? Residency::Touched : Residency::Untouched;
  reclaim.arena_ = nullptr;
}

std::size_t PayloadArena::resident_bytes() const {
  std::size_t n = 0;
  for (auto state : residency_) n += state != Residency::Untouched;
  return n * chunk_bytes_;
}

}  // namespace visio_schema::mcap
