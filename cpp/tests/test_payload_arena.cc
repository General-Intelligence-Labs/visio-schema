// PayloadArena — chunked memory for queued frames. The properties under test:
// a live record is never overwritten, a queue that keeps up stays inside its
// resident chunks, the chunks a backlog touched go back to the kernel as it
// drains, and records may be freed in any order.
#include "visio_schema/mcap/payload_arena.hpp"

#include <gtest/gtest.h>

#include <sys/mman.h>
#include <unistd.h>

#include <cstdint>
#include <deque>
#include <optional>
#include <random>
#include <string>
#include <utility>
#include <vector>

using visio_schema::mcap::PayloadArena;
using Record = PayloadArena::Record;

namespace {

constexpr std::size_t kChunk = 256u << 10;

std::string Bytes(std::size_t n, std::uint32_t seed) {
  std::string s(n, '\0');
  for (std::size_t i = 0; i < n; ++i)
    s[i] = static_cast<char>((seed * 2654435761u + i) >> 7);
  return s;
}

// Pages of [at, at + len) the kernel holds right now.
std::size_t ResidentPages(const char* at, std::size_t len) {
  const std::size_t page = static_cast<std::size_t>(::sysconf(_SC_PAGESIZE));
  std::vector<unsigned char> vec((len + page - 1) / page);
  EXPECT_EQ(::mincore(const_cast<char*>(at), len, vec.data()), 0);
  std::size_t n = 0;
  for (unsigned char v : vec) n += v & 1;
  return n;
}

}  // namespace

TEST(PayloadArena, ARecordReadsBackAsItWasPut) {
  PayloadArena arena(1u << 20, kChunk, 2);
  const std::string a = Bytes(1000, 1), b = Bytes(3000, 2);
  auto ra = arena.Put(a);
  auto rb = arena.Put(b);
  ASSERT_TRUE(ra && rb);
  EXPECT_EQ(ra->bytes(), a);
  EXPECT_EQ(rb->bytes(), b);
}

TEST(PayloadArena, AQueueThatKeepsUpStaysInItsResidentChunks) {
  PayloadArena arena(16u << 20, kChunk, 2);
  const std::string frame = Bytes(30000, 3);
  std::deque<Record> live;
  for (int i = 0; i < 5000; ++i) {
    auto r = arena.Put(frame);
    ASSERT_TRUE(r);
    live.push_back(std::move(*r));
    if (live.size() > 4) live.pop_front();  // a few frames in flight
  }
  EXPECT_LE(arena.resident_bytes(), 2 * kChunk);
}

TEST(PayloadArena, ABacklogsChunksGoBackAsItDrains) {
  PayloadArena arena(16u << 20, kChunk, 2);
  const std::string frame = Bytes(100000, 5);
  std::deque<Record> backlog;
  for (int i = 0; i < 60; ++i) backlog.push_back(std::move(*arena.Put(frame)));
  const char* first = backlog.front().bytes().data();
  ASSERT_GE(arena.resident_bytes(), 5u << 20);
  ASSERT_GT(ResidentPages(first + 2 * kChunk, 4u << 20), 0u);

  // Drains while new frames keep arriving (one per ten written): they go to
  // the lowest free chunk.
  for (int written = 0; !backlog.empty(); ++written) {
    backlog.pop_front();
    if (written % 10 == 0 && written < 60)
      backlog.push_back(std::move(*arena.Put(frame)));
  }
  EXPECT_LE(arena.resident_bytes(), 2 * kChunk);
  EXPECT_EQ(ResidentPages(first + 2 * kChunk, 4u << 20), 0u);
}

TEST(PayloadArena, AFullArenaRefusesUntilAChunkEmpties) {
  PayloadArena arena(4 * kChunk, kChunk, 1);
  const std::string half = Bytes(kChunk / 2, 4);
  std::vector<Record> live;
  while (auto r = arena.Put(half)) live.push_back(std::move(*r));
  ASSERT_EQ(live.size(), 8u);  // two per chunk, exactly
  EXPECT_FALSE(arena.Put(half));
  live.erase(live.begin() + 2);  // one record of a chunk: still not empty
  EXPECT_FALSE(arena.Put(half));
  live.erase(live.begin() + 2);  // its chunk-mate: the chunk is free
  EXPECT_TRUE(arena.Put(half));
}

TEST(PayloadArena, RefusesWhatItCannotHold) {
  PayloadArena arena(1u << 20, kChunk, 2);
  EXPECT_FALSE(arena.Put(""));
  EXPECT_FALSE(arena.Put(Bytes(kChunk + 1, 6)));
  PayloadArena none(0, kChunk, 2);
  EXPECT_FALSE(none.Put("x"));
  EXPECT_EQ(none.capacity(), 0u);
}

// Puts and frees in random order, as evictions from the middle of a queue
// do: every live record still reads back after every step.
TEST(PayloadArena, NoLiveRecordIsEverOverwritten) {
  PayloadArena arena(2u << 20, 64u << 10, 2);
  std::mt19937 rng(7);
  std::vector<std::pair<Record, std::string>> live;
  std::uint32_t seed = 0;
  for (int step = 0; step < 20000; ++step) {
    if (live.empty() || rng() % 3 != 0) {
      std::string b = Bytes(1 + rng() % 60000, ++seed);
      if (auto r = arena.Put(b)) live.emplace_back(std::move(*r), std::move(b));
    } else {
      live.erase(live.begin() + rng() % live.size());
    }
    for (const auto& [r, b] : live) ASSERT_EQ(r.bytes(), b) << "step " << step;
  }
}

TEST(PayloadArena, AMovedRecordFreesOnce) {
  PayloadArena arena(2 * kChunk, kChunk, 0);
  auto r = arena.Put(Bytes(kChunk, 8));
  ASSERT_TRUE(r);
  Record moved = std::move(*r);
  r.reset();                             // the moved-from shell frees nothing
  EXPECT_FALSE(arena.Put(Bytes(kChunk, 9)) &&
               arena.Put(Bytes(kChunk, 10)));  // chunk 0 is still held
}

// The backlog drains and nothing new arrives: even the chunk Put() was last
// filling goes back.
TEST(PayloadArena, ABacklogThatDrainsInSilenceKeepsNoChunk) {
  PayloadArena arena(16u << 20, kChunk, 2);
  std::deque<Record> backlog;
  for (int i = 0; i < 30; ++i)
    backlog.push_back(std::move(*arena.Put(Bytes(100000, 11))));
  backlog.clear();
  EXPECT_LE(arena.resident_bytes(), 2 * kChunk);
  EXPECT_TRUE(arena.Put(Bytes(100000, 12)));  // and the arena still works
}
