// MessageInbox tests — the inbound decoupling queue behind the native reader.
// Pins the new drop-oldest accounting (the starvation test deliberately uses a
// huge inbox so it never drops), the max_frames cap, FIFO order, and Close.
#include <gtest/gtest.h>

#include <cstdint>

#include "visio_schema/transport/message_inbox.hpp"
#include "visio_schema/transport/write_policy.hpp"

using visio_schema::transport::MessageInbox;
using visio_schema::transport::WritePolicy;
using visio_schema::wire::Message;

namespace {
Message Msg(std::uint32_t seq) {
  Message m;
  m.stream_id = 16;
  m.seq = seq;
  m.payload = "p";
  return m;
}
}  // namespace

TEST(MessageInbox, PopBatchReturnsFifoOrder) {
  MessageInbox box;
  for (std::uint32_t i = 0; i < 4; ++i) box.Push(Msg(i));
  auto batch = box.PopBatch(0, 0);
  ASSERT_EQ(batch.size(), 4u);
  for (std::uint32_t i = 0; i < 4; ++i) EXPECT_EQ(batch[i].seq, i);
  EXPECT_EQ(box.Dropped(), 0u);
}

TEST(MessageInbox, DropsOldestPastCapacityAndCounts) {
  MessageInbox box(WritePolicy::drop_oldest(3));
  for (std::uint32_t i = 0; i < 5; ++i) box.Push(Msg(i));
  // The oldest 2 are evicted; the freshest 3 (seq 2,3,4) remain.
  EXPECT_EQ(box.size(), 3u);
  EXPECT_EQ(box.Dropped(), 2u);
  auto batch = box.PopBatch(0, 0);
  ASSERT_EQ(batch.size(), 3u);
  EXPECT_EQ(batch.front().seq, 2u);
  EXPECT_EQ(batch.back().seq, 4u);
}

TEST(MessageInbox, MaxFramesCapsBatchAndKeepsRemainder) {
  MessageInbox box;
  for (std::uint32_t i = 0; i < 5; ++i) box.Push(Msg(i));
  auto first = box.PopBatch(0, 2);
  ASSERT_EQ(first.size(), 2u);
  EXPECT_EQ(first[0].seq, 0u);
  EXPECT_EQ(first[1].seq, 1u);
  auto rest = box.PopBatch(0, 0);
  ASSERT_EQ(rest.size(), 3u);
  EXPECT_EQ(rest.front().seq, 2u);
  EXPECT_EQ(rest.back().seq, 4u);
}

TEST(MessageInbox, CloseWakesAndReturnsEmpty) {
  MessageInbox box;
  box.Close();
  auto batch = box.PopBatch(1000, 0);  // must return at once, not block 1s
  EXPECT_TRUE(batch.empty());
  EXPECT_TRUE(box.closed());
}
