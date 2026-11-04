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
// The owner serializes Put(), Record release and reclaim completion.
// Only a claimed Reclaim::Run may execute outside that serialization. A live
// Record's bytes may be read without it: no live or claimed chunk is reused.
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string_view>
#include <utility>
#include <vector>

namespace visio_schema::mcap {

class PayloadArena {
 public:
  // The owner takes/completes under its serialization, runs outside it, and
  // keeps the arena alive until completion. The claimed chunk cannot be
  // reused.
  class Reclaim {
   public:
    Reclaim(Reclaim&& other) noexcept
        : arena_(std::exchange(other.arena_, nullptr)), chunk_(other.chunk_) {}
    Reclaim(const Reclaim&) = delete;
    Reclaim& operator=(const Reclaim&) = delete;
    int Run() const noexcept;  // 0 or errno; preserves/logs failures once

   private:
    friend class PayloadArena;
    Reclaim(PayloadArena* arena, std::size_t chunk)
        : arena_(arena), chunk_(chunk) {}
    PayloadArena* arena_;
    std::size_t chunk_;
  };

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
    // Consume this record under the owner's serialization. A last excess
    // chunk is quarantined until the caller immediately runs/completes it.
    std::optional<Reclaim> Release() noexcept;

   private:
    friend class PayloadArena;
    Record(PayloadArena* arena, std::size_t offset, std::size_t size)
        : arena_(arena), offset_(offset), size_(size) {}
    void Reset() noexcept;
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
  bool CanPut(std::size_t size) const;

  std::size_t capacity() const { return chunk_bytes_ * live_.size(); }
  // Bytes the arena may hold resident: every chunk written since it was last
  // handed back.
  std::size_t resident_bytes() const;

  void CompleteReclaim(Reclaim&& reclaim, int error);

 private:
  enum class Residency : std::uint8_t { Untouched, Touched, Reclaiming };
  static constexpr std::size_t kNoChunk = SIZE_MAX;
  std::optional<Reclaim> Free(std::size_t offset);
  std::size_t ChooseChunk(std::size_t size) const;
  std::size_t LowestFreeChunk() const;

  char* base_ = nullptr;
  const std::size_t chunk_bytes_;
  const std::size_t resident_chunks_;
  std::vector<std::uint32_t> live_;   // live records per chunk
  std::vector<Residency> residency_;  // bounded by the existing chunk count
  std::size_t current_ = kNoChunk;    // the chunk Put() bumps into
  std::size_t fill_ = 0;              // bytes used in current_
  std::atomic<bool> hand_back_failed_logged_{false};
};

}  // namespace visio_schema::mcap
