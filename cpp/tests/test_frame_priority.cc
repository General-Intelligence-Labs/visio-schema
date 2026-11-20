// Priority is rechecked after a partial video frame reaches its boundary.
#include <gtest/gtest.h>
#include <sys/socket.h>

#include <chrono>
#include <cstdint>
#include <string>
#include <thread>

#include "active_object_test_util.hpp"
#include "visio_schema/transport/framed_fd.hpp"
#include "visio_schema/transport/link.hpp"

namespace {
using namespace std::chrono_literals;
using visio_schema::transport::FramedFdEndpoint;
using visio_schema::transport::MakeFdPair;
using visio_schema::transport::WritePolicy;
using visio_schema::transport::test::ReadFramesFromFd;
using visio_schema::wire::Message;

class ProgressEndpoint : public FramedFdEndpoint {
 public:
  using FramedFdEndpoint::FramedFdEndpoint;
  std::uint64_t accepted() const { return accepted_total(); }
};

Message Sample(std::uint32_t stream, std::size_t size, bool bulk) {
  Message msg;
  msg.stream_id = stream;
  msg.payload = std::string(size, static_cast<char>(stream));
  msg.bulk = bulk;
  msg.keyframe = bulk;
  return msg;
}

void WaitForBytes(const ProgressEndpoint& endpoint, std::size_t count = 1) {
  const auto deadline = std::chrono::steady_clock::now() + 2s;
  while (endpoint.accepted() < count &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(1ms);
}

void CheckLanePriority(bool flush_video) {
  auto [sender, peer] = MakeFdPair();
  FramedFdEndpoint peer_owner(peer);
  const int buffer_bytes = 4096;
  ASSERT_EQ(::setsockopt(sender, SOL_SOCKET, SO_SNDBUF, &buffer_bytes,
                        sizeof(buffer_bytes)), 0);
  auto policy = WritePolicy::stale_eviction(
      2 * 1024 * 1024, 1000ms, WritePolicy::DrainMode::OneAtATime, 2048);
  ProgressEndpoint endpoint(sender, policy);
  Message video[] = {Sample(100, 512 * 1024, true),
                     Sample(101, 448 * 1024, true)};
  endpoint.SendBatch(video, 2);
  ASSERT_EQ(endpoint.dropped(), 0u);
  ASSERT_TRUE(video[0].framed);
  endpoint.Start({}, {});
  WaitForBytes(endpoint);
  ASSERT_GT(endpoint.accepted(), 0u);
  ASSERT_LT(endpoint.accepted(), video[0].framed->size());

  const Message control = Sample(102, 16, false);
  const Message timed = Sample(103, 16, false);
  const Message reliable = Sample(104, 16, false);
  endpoint.Send(control);
  endpoint.SendTimed(timed, [](Message&, std::int64_t) {}, {});
  ASSERT_EQ(endpoint.TrySendReliable(reliable),
            visio_schema::transport::Endpoint::SendResult::Accepted);
  if (flush_video) endpoint.RequestBulkFlush();
  const std::size_t count = flush_video ? 4u : 5u;
  const auto received = ReadFramesFromFd(peer, count);
  endpoint.Stop();
  ASSERT_EQ(received.size(), count);
  const Message expected[] = {video[0], timed, control, reliable, video[1]};
  for (std::size_t i = 0; i < received.size(); ++i) {
    EXPECT_EQ(received[i].stream_id, expected[i].stream_id);
    EXPECT_EQ(received[i].payload, expected[i].payload);
  }
  EXPECT_EQ(endpoint.dropped(), 0u);
}

TEST(FramePriority, VideoYieldsAfterPartialWrite) {
  CheckLanePriority(false);
}

TEST(FramePriority, FlushKeepsPriorityTraffic) {
  CheckLanePriority(true);
}

void CheckPartialFlush(std::chrono::milliseconds max_age, bool queue_video) {
  visio_schema::transport::test::InboundCollector inbox;
  auto [sender, reader] = MakeFdPair();
  FramedFdEndpoint peer(reader);
  const int buffer_bytes = 4096;
  ASSERT_EQ(::setsockopt(sender, SOL_SOCKET, SO_SNDBUF, &buffer_bytes,
                        sizeof(buffer_bytes)), 0);
  const auto policy = WritePolicy::stale_eviction(
      256 * 1024, max_age, WritePolicy::DrainMode::OneAtATime);
  ProgressEndpoint endpoint(sender, policy);
  const Message first = Sample(100, 128 * 1024, true);
  const Message fresh = Sample(102, 16, true);
  endpoint.Send(first);
  endpoint.Start({}, {});
  WaitForBytes(endpoint);
  ASSERT_GT(endpoint.accepted(), 0u);
  ASSERT_LT(endpoint.accepted(), first.framed->size());

  if (queue_video) {
    Message queued = Sample(101, 4096, true);
    queued.keyframe = false;
    endpoint.Send(queued);
  }
  endpoint.RequestBulkFlush();
  if (max_age > 0ms) std::this_thread::sleep_for(max_age + 1ms);

  peer.Start(inbox.fn(), {});
  ASSERT_GE(inbox.wait_for(1), 1u);
  WaitForBytes(endpoint, first.framed->size());
  ASSERT_GE(endpoint.accepted(), first.framed->size());
  endpoint.Send(fresh);
  inbox.wait_for(2);
  endpoint.Stop();
  peer.Stop();
  EXPECT_EQ(endpoint.dropped(), 0u);
  EXPECT_FALSE(endpoint.video_degraded());
  const auto received = inbox.messages();
  ASSERT_EQ(received.size(), 2u);
  EXPECT_EQ(received[0].stream_id, first.stream_id);
  EXPECT_EQ(received[0].payload, first.payload);
  EXPECT_EQ(received[1].stream_id, fresh.stream_id);
  EXPECT_EQ(received[1].payload, fresh.payload);
}

TEST(FramePriority, FlushAtPartialFrameBoundary) {
  CheckPartialFlush(0ms, true);
}

TEST(FramePriority, FlushBeforeAgeEviction) {
  CheckPartialFlush(1ms, true);
}

TEST(FramePriority, FlushWithNoQueuedVideo) {
  CheckPartialFlush(0ms, false);
}

TEST(FramePriority, CompletesControlBeforeTimed) {
  auto [sender, peer] = MakeFdPair();
  FramedFdEndpoint peer_owner(peer);
  const int buffer_bytes = 4096;
  ASSERT_EQ(::setsockopt(sender, SOL_SOCKET, SO_SNDBUF, &buffer_bytes,
                        sizeof(buffer_bytes)), 0);
  ProgressEndpoint endpoint(sender);
  const Message control = Sample(102, 512 * 1024, false);
  endpoint.Send(control);
  ASSERT_TRUE(control.framed);
  endpoint.Start({}, {});
  WaitForBytes(endpoint);
  ASSERT_GT(endpoint.accepted(), 0u);
  ASSERT_LT(endpoint.accepted(), control.framed->size());

  const Message video = Sample(100, 64 * 1024, true);
  const Message timed = Sample(103, 16, false);
  endpoint.Send(video);
  endpoint.SendTimed(timed, [](Message&, std::int64_t) {}, {});
  const auto received = ReadFramesFromFd(peer, 3);
  endpoint.Stop();
  ASSERT_EQ(received.size(), 3u);
  const Message expected[] = {control, timed, video};
  for (std::size_t i = 0; i < received.size(); ++i) {
    EXPECT_EQ(received[i].stream_id, expected[i].stream_id);
    EXPECT_EQ(received[i].payload, expected[i].payload);
  }
  EXPECT_EQ(endpoint.dropped(), 0u);
}
} // namespace
