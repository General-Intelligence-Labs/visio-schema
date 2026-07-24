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

// A keyframe is a SYNC POINT: every P-frame after it decodes only in its terms.
// Evicting one to bound latency is a false economy — the viewer then renders
// nothing until the next keyframe (a whole GOP), which on hardware showed up as
// a camera staying black for seconds after someone opened the live view. Stale
// P-frames around it are shed exactly as before, so latency stays bounded.
TEST(FramedOutbox, StaleEvictionKeepsKeyframesAndShedsThePFramesAroundThem) {
  FakeClock clk;
  FramedOutbox ob(WritePolicy::stale_eviction(/*max_bytes=*/1 << 20,
                                              std::chrono::microseconds(1000)),
                  clk.fn());
  auto stale_p = Frame(0x11, 4);
  ob.Enqueue(stale_p.data(), stale_p.size(), /*keyframe=*/false);
  auto stale_key = Frame(0x22, 4);
  ob.Enqueue(stale_key.data(), stale_key.size(), /*keyframe=*/true);
  auto stale_p2 = Frame(0x33, 4);
  ob.Enqueue(stale_p2.data(), stale_p2.size(), /*keyframe=*/false);

  clk.us = 5000;  // every frame above is now older than the 1 ms cap
  auto fresh = Frame(0x44, 4);
  ob.Enqueue(fresh.data(), fresh.size(), /*keyframe=*/false);

  FakeSink sink;
  while (ob.HasPending()) ASSERT_TRUE(ob.Drain(sink.fn()));

  // The keyframe survived its own staleness; both stale P-frames did not.
  std::vector<std::uint8_t> expected = stale_key;
  expected.insert(expected.end(), fresh.begin(), fresh.end());
  EXPECT_EQ(sink.received, expected);
  EXPECT_EQ(ob.Dropped(), 2u);
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

// ---------------------------------------------------------------- shared ---
// Frame-once fanout: one immutable SharedFrame feeds N outboxes by refcount.

TEST(FramedOutboxShared, OneBufferFansOutToTwoOutboxesIntact) {
  auto buf = std::make_shared<const std::vector<std::uint8_t>>(
      Frame(0xAB, 4096));
  FramedOutbox a(WritePolicy::drop_oldest());
  FramedOutbox b(WritePolicy::drop_oldest());
  EXPECT_TRUE(a.Enqueue(buf));
  EXPECT_TRUE(b.Enqueue(buf));
  EXPECT_EQ(buf.use_count(), 3);  // caller + both queues; no copies

  FakeSink sa, sb;
  while (a.HasPending()) ASSERT_TRUE(a.Drain(sa.fn()));
  while (b.HasPending()) ASSERT_TRUE(b.Drain(sb.fn()));
  EXPECT_EQ(sa.received, *buf);
  EXPECT_EQ(sb.received, *buf);
}

TEST(FramedOutboxShared, OneAtATimePartialWriteFinishesSharedFrame) {
  auto buf = std::make_shared<const std::vector<std::uint8_t>>(
      Frame(0xCD, 100));
  FramedOutbox ob(WritePolicy::stale_eviction(
      1 << 20, std::chrono::microseconds(0),
      WritePolicy::DrainMode::OneAtATime));
  EXPECT_TRUE(ob.Enqueue(buf));

  FakeSink sink;
  sink.accept_per_call = 30;  // force multiple partial writes
  while (ob.HasPending()) ASSERT_TRUE(ob.Drain(sink.fn()));
  EXPECT_EQ(sink.received, *buf);
}

TEST(FramedOutboxShared, EvictionDropsRefcountNotTheSharedBuffer) {
  auto big = std::make_shared<const std::vector<std::uint8_t>>(
      Frame(0xEE, 900));
  FramedOutbox ob(WritePolicy::stale_eviction(
      /*max_bytes=*/1000, std::chrono::microseconds(0)));
  EXPECT_TRUE(ob.Enqueue(big));
  // A second 900-byte frame overflows the 1000-byte cap: the first is
  // evicted — but only the queue's reference dies, the caller's buffer
  // (shared with sibling sinks) is untouched.
  EXPECT_TRUE(ob.Enqueue(big));
  EXPECT_EQ(ob.Dropped(), 1u);
  EXPECT_EQ(*big, Frame(0xEE, 900));
  EXPECT_EQ(ob.PendingBytes(), 900u);
}

TEST(FramedOutboxShared, ClearMidFlightResetsSharedInFlight) {
  auto buf = std::make_shared<const std::vector<std::uint8_t>>(
      Frame(0x77, 64));
  FramedOutbox ob(WritePolicy::stale_eviction(
      1 << 20, std::chrono::microseconds(0),
      WritePolicy::DrainMode::OneAtATime));
  EXPECT_TRUE(ob.Enqueue(buf));
  FakeSink sink;
  sink.accept_per_call = 16;
  ASSERT_TRUE(ob.Drain(sink.fn()));  // partial: 16 of 64 on the wire
  ASSERT_TRUE(ob.InFlightActive());
  ob.Clear();
  EXPECT_FALSE(ob.InFlightActive());
  EXPECT_FALSE(ob.HasPending());
  EXPECT_EQ(buf.use_count(), 1);  // outbox released its reference
}

TEST(FramedOutboxShared, BatchAllCoalescesSharedFramesIntoOneWrite) {
  auto a = std::make_shared<const std::vector<std::uint8_t>>(Frame(0x01, 10));
  auto b = std::make_shared<const std::vector<std::uint8_t>>(Frame(0x02, 20));
  FramedOutbox ob(WritePolicy::stale_eviction(
      1 << 20, std::chrono::microseconds(0), WritePolicy::DrainMode::BatchAll));
  EXPECT_TRUE(ob.Enqueue(a));
  EXPECT_TRUE(ob.Enqueue(b));

  FakeSink sink;
  ASSERT_TRUE(ob.Drain(sink.fn()));
  std::vector<std::uint8_t> want = Frame(0x01, 10);
  const auto tail = Frame(0x02, 20);
  want.insert(want.end(), tail.begin(), tail.end());
  EXPECT_EQ(sink.received, want);
  EXPECT_FALSE(ob.HasPending());
}
