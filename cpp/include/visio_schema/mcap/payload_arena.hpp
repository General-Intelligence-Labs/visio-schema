// PayloadArena — the bytes a recorder has queued but not yet written, held in
// memory the recorder owns rather than in the producers' heap buffers.
//
// Why: a writer queue that holds references to producer buffers grows the
// HEAP whenever storage stalls, and most allocators never hand freed heap
// back to the kernel. One stall then costs its peak backlog for the rest of
// the process's life. This arena reserves its whole capacity as address
// space up front (no RAM until a byte is written), lives in its first few
// chunks while storage keeps up, and returns every chunk above those to the
// kernel the moment a backlog has drained out of it.
//
// Layout: the reservation is cut into equal chunks. Records are bump-allocated
// inside the current chunk and never span two; each chunk counts its live
// records, and an emptied chunk is free for reuse. A new chunk is always the
// lowest free one, which is what keeps a steady state inside the first few.
//
// Not thread-safe: the owner serializes Put() and every Record's destruction
// (the endpoint holds its queue lock for both). A Record's bytes may be read
// without that lock for as long as the Record lives: nothing writes or trims
// a chunk while it holds a live record.
#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string_view>
#include <vector>

namespace visio_schema::mcap {

class PayloadArena {
 public:
  // A record's bytes, owned: destroying it frees its place in the arena.
  class Record {
   public:
    Record(Record&& o) noexcept
        : arena_(o.arena_), offset_(o.offset_), size_(o.size_) {
      o.arena_ = nullptr;
    }
    Record& operator=(Record&& o) noexcept;
    Record(const Record&) = delete;
    Record& operator=(const Record&) = delete;
    ~Record();

    std::string_view bytes() const;

   private:
    friend class PayloadArena;
    Record(PayloadArena* arena, std::size_t offset, std::size_t size)
        : arena_(arena), offset_(offset), size_(size) {}
    PayloadArena* arena_;
    std::size_t offset_;
    std::size_t size_;
  };

  // `capacity`: address space reserved, the most that can be held at once.
  // `chunk_bytes`: the largest record; anything bigger is refused.
  // `resident_chunks`: chunks kept mapped while empty, the steady-state set.
  // A failed reservation leaves the arena empty: every Put() then fails and
  // the caller keeps its own buffers.
  PayloadArena(std::size_t capacity, std::size_t chunk_bytes,
               std::size_t resident_chunks);
  ~PayloadArena();

  PayloadArena(const PayloadArena&) = delete;
  PayloadArena& operator=(const PayloadArena&) = delete;

  // Copy `bytes` in, or nullopt when there is no room for them now, when they
  // are larger than a chunk, or when they are empty.
  std::optional<Record> Put(std::string_view bytes);

  std::size_t capacity() const { return chunk_bytes_ * live_.size(); }
  // Bytes the arena may hold resident: every chunk written since it was last
  // handed back.
  std::size_t resident_bytes() const;

 private:
  static constexpr std::size_t kNoChunk = SIZE_MAX;
  void Free(std::size_t offset);
  std::size_t LowestFreeChunk() const;
  void HandBack(std::size_t chunk);

  char* base_ = nullptr;
  const std::size_t chunk_bytes_;
  const std::size_t resident_chunks_;
  std::vector<std::uint32_t> live_;    // live records per chunk
  std::vector<bool> touched_;          // written since last handed back
  std::size_t current_ = kNoChunk;     // the chunk Put() bumps into
  std::size_t fill_ = 0;               // bytes used in current_
  bool hand_back_failed_logged_ = false;
};

}  // namespace visio_schema::mcap
