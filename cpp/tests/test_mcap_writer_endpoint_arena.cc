// McapWriterEndpoint with a byte-bounded policy queues frames in its own arena
// (payload_arena.hpp): the producer's buffer is let go at Send(), and what the
// file receives is byte for byte what was sent. A policy without a byte bound
// holds the producer's buffer.
#include "visio_schema/mcap/writer_endpoint.hpp"

#include <gtest/gtest.h>

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "mcap_writer_test_util.hpp"
#include "visio_schema/routing/channel.hpp"
#include "visio_schema/wire/control.hpp"
#include "visio_schema/wire/time.hpp"

using namespace visio_schema::mcap;
using namespace mcap_test;
using visio_schema::Channel;
using visio_schema::kFirstDynamic;
using visio_schema::transport::WritePolicy;
using visio_schema::wire::Message;
using visio_schema::wire::Payload;

namespace {

class EndpointArena : public ::testing::Test {
 protected:
  std::string path;
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/camera/0")}};
  StreamResolver resolve = [this](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };

  void SetUp() override {
    path = TempPath(std::string("visio_mcap_test_arena_")+::testing::UnitTest::GetInstance()->current_test_info()->name()+".mcap");
    std::remove(path.c_str());
  }
  void TearDown() override { std::remove(path.c_str()); }

  static Message Frame(std::shared_ptr<const std::string> bytes) {
    Message m;
    m.stream_id = kFirstDynamic;
    m.payload = Payload(std::move(bytes));
    return m;
  }
};

WritePolicy RecorderPolicy() {
  return WritePolicy::stale_eviction(16u << 20, std::chrono::milliseconds(0));
}

}  // namespace

// Sent before Start(), so nothing has drained it: whatever holds the buffer
// after Send() is the queue.
TEST_F(EndpointArena, ABoundedQueueLetsGoOfTheProducersBufferAtSend) {
  McapWriterEndpoint ep(path, resolve, 0, 0.0, RecorderPolicy());
  auto frame = std::make_shared<const std::string>(50000, 'v');
  ep.Send(Frame(frame));
  EXPECT_EQ(frame.use_count(), 1);
  ep.Start(nullptr, nullptr);
  ep.Stop();
}

TEST_F(EndpointArena, AnUnboundedQueueHoldsTheProducersBuffer) {
  McapWriterEndpoint ep(path, resolve, 0, 0.0, WritePolicy::lossless());
  auto frame = std::make_shared<const std::string>(50000, 'v');
  ep.Send(Frame(frame));
  EXPECT_EQ(frame.use_count(), 2);
  ep.Start(nullptr, nullptr);
  ep.Stop();
}

// Many frames of varied sizes through the arena, across many of its chunks:
// each lands in the file intact and in order.
TEST_F(EndpointArena, EveryFrameReachesTheFileIntactAndInOrder) {
  std::vector<std::string> sent;
  {
    McapWriterEndpoint ep(path, resolve, 0, 0.0, RecorderPolicy());
    ep.Start(nullptr, nullptr);
    for (int i = 0; i < 400; ++i) {
      std::string body = "<frame " + std::to_string(i) + ">";
      body += std::string(1000 + (i * 7919) % 60000,
                          static_cast<char>('a' + i % 26));
      body += "</frame " + std::to_string(i) + ">";
      sent.push_back(body);
      ep.Send(Frame(std::make_shared<const std::string>(body)));
    }
    ep.Stop();
    EXPECT_EQ(ep.dropped_frames(), 0u);
  }
  const std::string file = SlurpFile(path);
  std::size_t from = 0;
  for (std::size_t i = 0; i < sent.size(); ++i) {
    const std::size_t at = file.find(sent[i], from);
    ASSERT_NE(at, std::string::npos)
        << "frame " << i << " missing or out of order";
    from = at + sent[i].size();
  }
}

namespace {

// Each frame's body is unique, so its position in the file proves its order.
std::string Body(int i, std::size_t n) {
  std::string b = "<frame " + std::to_string(i) + ">";
  b += std::string(n, static_cast<char>('a' + i % 26));
  return b + "</frame " + std::to_string(i) + ">";
}

void ExpectInFileInOrder(const std::string& path,
                         const std::vector<std::string>& bodies) {
  const std::string file = SlurpFile(path);
  std::size_t from = 0;
  for (std::size_t i = 0; i < bodies.size(); ++i) {
    const std::size_t at = file.find(bodies[i], from);
    ASSERT_NE(at, std::string::npos) << "body " << i;
    from = at + bodies[i].size();
  }
}

}  // namespace

// The queue is held at its bound, so each new frame evicts the oldest: the
// evicted frames' records must free their chunks, or the arena would fill
// and every later frame would land in the heap. The survivors reach the file
// intact.
TEST_F(EndpointArena, EvictionGivesTheArenaItsSpaceBack) {
  McapWriterEndpoint ep(path, resolve, 0, 0.0,
                        WritePolicy::stale_eviction(256u << 10,
                                                    std::chrono::milliseconds(0)));
  std::vector<std::string> kept;
  for (int i = 0; i < 40; ++i) {
    auto frame = std::make_shared<const std::string>(Body(i, 64u << 10));
    ep.Send(Frame(frame));
    EXPECT_EQ(frame.use_count(), 1) << "frame " << i << " fell back to heap";
    if (i >= 37) kept.push_back(*frame);
  }
  EXPECT_EQ(ep.stats().arena_fallbacks, 0u);
  ep.Start(nullptr, nullptr);
  ep.Stop();
  ExpectInFileInOrder(path, kept);
}

// A frame larger than an arena chunk is held in its producer's buffer,
// counted, and still recorded.
TEST_F(EndpointArena, AFrameTheArenaCannotTakeIsHeldCountedAndRecorded) {
  McapWriterEndpoint ep(path, resolve, 0, 0.0, RecorderPolicy());
  auto big = std::make_shared<const std::string>(Body(0, 300u << 10));
  ep.Send(Frame(big));
  EXPECT_EQ(big.use_count(), 2);
  EXPECT_EQ(ep.stats().arena_fallbacks, 1u);
  ep.Start(nullptr, nullptr);
  ep.Stop();
  ExpectInFileInOrder(path, {*big});
}

// A long take through a small arena, drained as it goes: the arena keeps up,
// so no frame ever falls back to the heap.
TEST_F(EndpointArena, ALongTakeNeverFallsBackToTheHeap) {
  McapWriterEndpoint ep(path, resolve, 0, 0.0,
                        WritePolicy::stale_eviction(1u << 20,
                                                    std::chrono::milliseconds(0)));
  ep.Start(nullptr, nullptr);
  for (int i = 0; i < 800; ++i) {
    ep.Send(Frame(std::make_shared<const std::string>(Body(i, 64u << 10))));
    if (i % 8 == 7)
      while (ep.pending_bytes() > 0) std::this_thread::yield();
  }
  ep.Stop();
  EXPECT_EQ(ep.stats().arena_fallbacks, 0u);
  EXPECT_EQ(ep.dropped_frames(), 0u);
}

TEST_F(EndpointArena, OnlyAByteBoundedPolicyReservesAnArena) {
  McapWriterEndpoint bounded(path, resolve, 0, 0.0, RecorderPolicy());
  EXPECT_EQ(bounded.stats().arena_capacity_bytes, 32u << 20);
  McapWriterEndpoint by_depth(path + ".2", resolve, 0, 0.0,
                              WritePolicy::drop_oldest(64));
  EXPECT_EQ(by_depth.stats().arena_capacity_bytes, 0u);
  std::remove((path + ".2").c_str());
}

TEST_F(EndpointArena, DeferredRotationOwnsPayloadsAfterTheirArenaChunkIsReused) {
  for (const std::string finish : {"aligned", "quiet", "stop"}) {
    SCOPED_TRACE(finish);
    const std::string stem = "arena_continuation_" + finish;
    RemoveParts(stem);
    auto a = MakeChannel(16, "/ego/camera/0");
    auto b = MakeChannel(17, "/ego/camera/1");
    const auto imu = MakeChannel(18, "/imu");
    a.schema_name = b.schema_name = "foxglove.CompressedVideo";
    const auto resolver = [&](std::uint32_t id) -> const Channel* {
      return id == a.id ? &a : id == b.id ? &b : &imu;
    };
    McapRotationPolicy rotation;
    rotation.video_ids = {a.id, b.id};
    rotation.max_wait = finish == "quiet" ? std::chrono::milliseconds(250)
                                          : std::chrono::seconds(5);
    rotation.max_staged_bytes = 256u << 10;
    constexpr std::size_t kPrimeBytes = 64u << 10;
    constexpr std::size_t kStagedBytes = 32u << 10;
    constexpr std::size_t kPartBytes = 16 * kPrimeBytes;
    // Exactly one arena chunk: after each entry is freed the next Send must
    // overwrite the same storage, while rotation still owns its earlier bytes.
    McapWriterEndpoint ep(TempPath(stem + ".mcap"), resolver, kPartBytes, 0,
                          WritePolicy::stale_eviction(128u << 10,
                                                      std::chrono::milliseconds(0)),
                          {}, true, 0, 0, {}, {}, {}, {}, rotation);
    ASSERT_EQ(ep.stats().arena_capacity_bytes, 256u << 10);
    auto wait_for = [&](auto ready) {
      const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
      while (!ready() && std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
      return ready();
    };
    std::vector<std::string> expected;
    auto send = [&](std::uint32_t id, std::size_t size, bool keyframe, unsigned seq) {
      std::string body = "<" + std::to_string(id) + ":" + std::to_string(seq) + ">";
      body.resize(size, static_cast<char>('a' + expected.size()));
      expected.push_back(body);
      auto producer = std::make_shared<const std::string>(std::move(body));
      Message message;
      message.stream_id = id;
      message.seq = seq;
      message.keyframe = keyframe;
      if(keyframe)message.sync_point={1,1,seq};
      message.payload = Payload(producer);
      visio_schema::SetTimestampNs(&message.timestamp, seq * 33333333LL);
      ep.Send(message);
      EXPECT_EQ(producer.use_count(), 2) << "the endpoint fell back to the producer";
    };
    ep.Start(nullptr, nullptr);
    for (unsigned seq = 0; seq < 8; ++seq) {
      send(a.id, kPrimeBytes, seq == 0, seq);
      if(seq)ASSERT_TRUE(wait_for([&] { return ep.pending_bytes() == 0; }));
      send(b.id, kPrimeBytes, seq == 0, seq);
      ASSERT_TRUE(wait_for([&] { return ep.pending_bytes() == 0; }));
    }
    ASSERT_EQ(ep.bytes_written(), kPartBytes);
    send(a.id, kStagedBytes, true, 8);
    // Staging owns one payload plus bookkeeping. A second payload's worth
    // would mean the original arena Entry is still in flight.
    ASSERT_TRUE(wait_for([&] {
      const auto pending = ep.pending_bytes();
      return ep.stats().writes == 17 && pending > kStagedBytes && pending < 2 * kStagedBytes;
    }));
    send(imu.id, kStagedBytes, false, 8);
    ASSERT_TRUE(wait_for([&] {
      const auto pending = ep.pending_bytes();
      return ep.stats().writes == 18 && pending > 2 * kStagedBytes && pending < 3 * kStagedBytes;
    }));
    ASSERT_EQ(ep.bytes_written(), kPartBytes);
    if (finish == "aligned") send(b.id, kStagedBytes, true, 8);
    if (finish != "stop")
      ASSERT_TRUE(wait_for([&] { return ep.pending_bytes() == 0; }));
    ep.Stop();
    EXPECT_FALSE(ep.write_failed());
    EXPECT_EQ(ep.pending_bytes(), 0u);
    EXPECT_EQ(ep.stats().dropped, 0u);
    EXPECT_EQ(ep.stats().arena_fallbacks, 0u);
    EXPECT_EQ(ep.stats().keyframe_gated, 0u);
    EXPECT_EQ(ep.stats().rotation.aligned, finish == "aligned" ? 1u : 0u);
    EXPECT_EQ(ep.stats().rotation.cancelled, finish == "aligned" ? 0u : 1u);
    EXPECT_LE(ep.stats().rotation.max_staged_bytes, rotation.max_staged_bytes);
    EXPECT_EQ(ep.bytes_written(), kPartBytes + (finish == "aligned" ? 3 : 2) * kStagedBytes);
    ASSERT_EQ(PartCount(stem), finish == "aligned" ? 2 : 1);
    std::string recorded;
    for (int part = 0; part < PartCount(stem); ++part) {
      const auto bytes = SlurpFile(PartPath(stem, part));
      EXPECT_TRUE(EndsWithMcapMagic(bytes));
      recorded += bytes;
    }
    std::size_t from = 0;
    for (const auto& body : expected) {
      const auto at = recorded.find(body, from);
      ASSERT_NE(at, std::string::npos) << body.substr(0, 16);
      from = at + body.size();
    }
    RemoveParts(stem);
  }
}
