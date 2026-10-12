#include "visio_schema/mcap/payload_arena.hpp"

#include <sys/mman.h>

#include <cerrno>
#include <cstring>

#include "visio_schema/log.hpp"

namespace visio_schema::mcap {

PayloadArena::Record& PayloadArena::Record::operator=(Record&& o) noexcept {
  if (this != &o) {
    if (arena_) arena_->Free(offset_);
    arena_ = o.arena_;
    offset_ = o.offset_;
    size_ = o.size_;
    o.arena_ = nullptr;
  }
  return *this;
}

PayloadArena::Record::~Record() {
  if (arena_) arena_->Free(offset_);
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
  touched_.assign(chunks, false);
}

PayloadArena::~PayloadArena() {
  if (base_) ::munmap(base_, capacity());
}

std::optional<PayloadArena::Record> PayloadArena::Put(std::string_view bytes) {
  const std::size_t len = bytes.size();
  if (len == 0 || len > chunk_bytes_ || !base_) return std::nullopt;
  if (current_ == kNoChunk || fill_ + len > chunk_bytes_) {
    // An emptied current chunk restarts at 0, so a chunk left here still
    // holds a record, and its last Free() hands it back.
    const std::size_t next = LowestFreeChunk();
    if (next == kNoChunk) return std::nullopt;
    current_ = next;
    fill_ = 0;
  }
  const std::size_t offset = current_ * chunk_bytes_ + fill_;
  std::memcpy(base_ + offset, bytes.data(), len);
  fill_ += len;
  ++live_[current_];
  touched_[current_] = true;
  return Record(this, offset, len);
}

void PayloadArena::Free(std::size_t offset) {
  const std::size_t chunk = offset / chunk_bytes_;
  if (--live_[chunk] != 0) return;
  // Emptied, the current chunk fills again from its start, unless it lies
  // above the resident set: then it is given up, so a backlog that drains
  // with nothing arriving does not keep its last chunk.
  if (chunk == current_) {
    fill_ = 0;
    if (chunk < resident_chunks_) return;
    current_ = kNoChunk;
  }
  HandBack(chunk);
}

std::size_t PayloadArena::LowestFreeChunk() const {
  for (std::size_t c = 0; c < live_.size(); ++c)
    if (c != current_ && live_[c] == 0) return c;
  return kNoChunk;
}

// An emptied chunk outside the resident set goes back to the kernel now:
// anonymous private pages read as zero if written again.
void PayloadArena::HandBack(std::size_t chunk) {
  if (chunk < resident_chunks_ || !touched_[chunk]) return;
  if (::madvise(base_ + chunk * chunk_bytes_, chunk_bytes_, MADV_DONTNEED) != 0) {
    if (!hand_back_failed_logged_) {
      hand_back_failed_logged_ = true;
      log::Write(log::Severity::kWarning,
                 "PayloadArena: madvise(DONTNEED) failed: %s — emptied chunks "
                 "stay with the process",
                 std::strerror(errno));
    }
    return;
  }
  touched_[chunk] = false;
}

std::size_t PayloadArena::resident_bytes() const {
  std::size_t n = 0;
  for (bool t : touched_) n += t;
  return n * chunk_bytes_;
}

}  // namespace visio_schema::mcap
