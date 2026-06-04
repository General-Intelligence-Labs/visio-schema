// FramedOutbox — bounded-queue backpressure + the in-flight/eviction discipline
// ported from umi_channel.hpp. The headline test is the H.265-corruption
// regression: eviction must never splice a frame already committed to the wire.
#include "visio_schema/transport/framed_outbox.hpp"

#include <gtest/gtest.h>

#include <cstdint>
#include <vector>

using visio_schema::transport::FramedOutbox;
using visio_schema::transport::WritePolicy;

namespace {

// A configurable non-blocking sink. `accept_per_call` caps bytes taken per
// write (0 = unlimited); `dead` makes the next write report a broken link.
struct FakeSink {
  std::vector<std::uint8_t> received;
  std::size_t accept_per_call = 0;  // 0 = accept everything offered
  bool eagain = false;              // report would-block (0 bytes) once
  bool dead = false;

  FramedOutbox::WriteFn fn() {
    return [this](const std::uint8_t* p, std::size_t n) -> long {
      if (dead) return -1;
      if (eagain) { eagain = false; return 0; }
      std::size_t take = (accept_per_call && accept_per_call < n) ? accept_per_call : n;
      received.insert(received.end(), p, p + take);
      return static_cast<long>(take);
    };
  }
};

std::vector<std::uint8_t> Frame(std::uint8_t byte, std::size_t len) {
  return std::vector<std::uint8_t>(len, byte);
}

// A clock the test advances by hand (microseconds).
struct FakeClock {
  std::int64_t us = 0;
  FramedOutbox::NowFn fn() { return [this] { return us; }; }
};

}  // namespace

TEST(FramedOutbox, DropOldestShedsOldestKeepsNewest) {
  FramedOutbox ob(WritePolicy::drop_oldest(/*depth=*/2));
  auto a = Frame(0xA1, 4), b = Frame(0xB2, 4), c = Frame(0xC3, 4);
  EXPECT_TRUE(ob.Enqueue(a.data(), a.size()));
  EXPECT_TRUE(ob.Enqueue(b.data(), b.size()));
  EXPECT_TRUE(ob.Enqueue(c.data(), c.size()));  // pushes A out
  EXPECT_EQ(ob.QueuedFrames(), 2u);

  FakeSink sink;
  while (ob.HasPending()) ASSERT_TRUE(ob.Drain(sink.fn()));
  // A dropped; B then C delivered intact, in order.
  std::vector<std::uint8_t> want;
  want.insert(want.end(), b.begin(), b.end());
  want.insert(want.end(), c.begin(), c.end());
  EXPECT_EQ(sink.received, want);
}

TEST(FramedOutbox, DropOnFailRejectsNewFrameWhenFull) {
  FramedOutbox ob(WritePolicy::drop_on_fail(/*depth=*/1));
  auto a = Frame(0xA1, 4), b = Frame(0xB2, 4);
  EXPECT_TRUE(ob.Enqueue(a.data(), a.size()));
  EXPECT_FALSE(ob.Enqueue(b.data(), b.size()));  // queue full -> reject B
  EXPECT_EQ(ob.QueuedFrames(), 1u);

  FakeSink sink;
  while (ob.HasPending()) ASSERT_TRUE(ob.Drain(sink.fn()));
  EXPECT_EQ(sink.received, a);  // only A survives
}

TEST(FramedOutbox, StaleEvictionBoundsBytes) {
  // 10-byte cap; each frame is 4 bytes -> at most 2 fit.
  FramedOutbox ob(WritePolicy::stale_eviction(/*max_bytes=*/10,
                                              std::chrono::microseconds(0)));
  for (std::uint8_t i = 0; i < 4; ++i) {
    auto f = Frame(0xF0 + i, 4);
    ob.Enqueue(f.data(), f.size());
  }
  EXPECT_LE(ob.PendingBytes(), 10u);
  EXPECT_EQ(ob.QueuedFrames(), 2u);  // oldest two evicted
}

TEST(FramedOutbox, StaleEvictionDropsAgedFramesOnDrain) {
  FakeClock clk;
  FramedOutbox ob(WritePolicy::stale_eviction(/*max_bytes=*/1 << 20,
                                              std::chrono::microseconds(1000)),
                  clk.fn());
  auto old = Frame(0x11, 4);
  ob.Enqueue(old.data(), old.size());  // enqueued at t=0
  clk.us = 5000;                       // 5 ms later -> older than 1 ms cap
  auto fresh = Frame(0x22, 4);
  ob.Enqueue(fresh.data(), fresh.size());  // enqueued at t=5000

  FakeSink sink;
  ASSERT_TRUE(ob.Drain(sink.fn()));  // phase-2 age eviction drops `old`
  while (ob.HasPending()) ASSERT_TRUE(ob.Drain(sink.fn()));
  EXPECT_EQ(sink.received, fresh);  // stale frame never reached the wire
}

// THE regression: a frame partially committed to the wire (in_flight_) must be
// finished intact even when later enqueues overflow max_depth and force
// eviction. Eviction may only shed the uncommitted queue. (Original H.265 bug.)
TEST(FramedOutbox, EvictionNeverSplicesInFlightFrame) {
  FramedOutbox ob(WritePolicy::drop_oldest(/*depth=*/2));  // OneAtATime
  auto A = Frame(0xAA, 10), B = Frame(0xBB, 10), C = Frame(0xCC, 10),
       D = Frame(0xDD, 10);

  // Enqueue A, then a drain that commits only the first 4 bytes of A.
  ASSERT_TRUE(ob.Enqueue(A.data(), A.size()));
  FakeSink sink;
  sink.accept_per_call = 4;
  ASSERT_TRUE(ob.Drain(sink.fn()));     // A now in-flight, 4/10 written
  EXPECT_EQ(ob.PendingBytes(), 6u);     // remainder of A still committed

  // Overflow the queue while A is mid-write. depth=2 -> B is dropped, [C,D] kept.
  ASSERT_TRUE(ob.Enqueue(B.data(), B.size()));
  ASSERT_TRUE(ob.Enqueue(C.data(), C.size()));
  ASSERT_TRUE(ob.Enqueue(D.data(), D.size()));

  // Drain to completion with an unbounded sink.
  sink.accept_per_call = 0;
  while (ob.HasPending()) ASSERT_TRUE(ob.Drain(sink.fn()));

  // The wire must show: complete A, then complete C, then complete D. B gone.
  // Critically A is whole (10 bytes) — never truncated by the eviction.
  std::vector<std::uint8_t> want;
  want.insert(want.end(), A.begin(), A.end());
  want.insert(want.end(), C.begin(), C.end());
  want.insert(want.end(), D.begin(), D.end());
  EXPECT_EQ(sink.received, want);
}

TEST(FramedOutbox, BatchAllCoalescesQueuedFrames) {
  FramedOutbox ob(WritePolicy::stale_eviction(1 << 20, std::chrono::microseconds(0),
                                              WritePolicy::DrainMode::BatchAll));
  auto a = Frame(0x01, 3), b = Frame(0x02, 3), c = Frame(0x03, 3);
  ob.Enqueue(a.data(), a.size());
  ob.Enqueue(b.data(), b.size());
  ob.Enqueue(c.data(), c.size());

  // Count write calls: BatchAll should drain all three in a single write.
  int calls = 0;
  FakeSink sink;
  auto base = sink.fn();
  auto counting = [&](const std::uint8_t* p, std::size_t n) -> long {
    ++calls;
    return base(p, n);
  };
  ASSERT_TRUE(ob.Drain(counting));
  EXPECT_FALSE(ob.HasPending());
  EXPECT_EQ(calls, 1);  // one coalesced write
  EXPECT_EQ(sink.received.size(), 9u);
}

TEST(FramedOutbox, DeadLinkReportedFalse) {
  FramedOutbox ob(WritePolicy::drop_oldest(8));
  auto a = Frame(0xAA, 4);
  ob.Enqueue(a.data(), a.size());
  FakeSink sink;
  sink.dead = true;
  EXPECT_FALSE(ob.Drain(sink.fn()));  // link reported dead
}

TEST(FramedOutbox, EagainKeepsFrameForRetry) {
  FramedOutbox ob(WritePolicy::drop_oldest(8));
  auto a = Frame(0xAA, 4);
  ob.Enqueue(a.data(), a.size());
  FakeSink sink;
  sink.eagain = true;                 // first write would-blocks
  ASSERT_TRUE(ob.Drain(sink.fn()));   // not dead, just deferred
  EXPECT_TRUE(ob.HasPending());
  EXPECT_TRUE(sink.received.empty());
  ASSERT_TRUE(ob.Drain(sink.fn()));   // retry succeeds
  EXPECT_EQ(sink.received, a);
}

TEST(FramedOutbox, ClearDropsEverything) {
  FramedOutbox ob(WritePolicy::drop_oldest(8));
  auto a = Frame(0xAA, 10);
  ob.Enqueue(a.data(), a.size());
  FakeSink sink;
  sink.accept_per_call = 4;
  ASSERT_TRUE(ob.Drain(sink.fn()));  // partial -> some in-flight
  ASSERT_TRUE(ob.HasPending());
  ob.Clear();
  EXPECT_FALSE(ob.HasPending());
  EXPECT_EQ(ob.PendingBytes(), 0u);
}
