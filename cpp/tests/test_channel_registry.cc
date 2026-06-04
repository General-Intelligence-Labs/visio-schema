// ChannelRegistry tests — the single-source topic/schema table (no bus): own
// outputs, learned channels by id, the unique-topic invariant, Forget, the
// Accept/consume path, and the DeviceInfo announce round-trip. Mirrors
// python/visio_schema/tests/test_channel_registry.py.
#include "visio_schema/routing/registry.hpp"

#include <gtest/gtest.h>

#include "visio_schema/wire/v1/header.pb.h"

using visio_schema::Channel;
using visio_schema::kFirstDynamic;
using visio_schema::routing::ChannelRegistry;
using visio_schema::routing::DuplicateTopicError;
using visio_schema::wire::Message;

namespace {

constexpr std::uint32_t kDeviceInfo =
    visio_schema_wire_v1_ControlStream_CONTROL_STREAM_DEVICE_INFO;
constexpr std::uint32_t kHeartbeat =
    visio_schema_wire_v1_ControlStream_CONTROL_STREAM_HEARTBEAT;

Channel MakeChannel(std::uint32_t id, const std::string& topic) {
  Channel c;
  c.id = id;
  c.topic = topic;
  c.schema_name = "visio_schema.sensor.v1.ImuRaw";
  return c;
}

Message Announce(std::uint32_t id, const std::string& topic) {
  Message m;
  m.stream_id = kDeviceInfo;
  m.payload = ChannelRegistry::Encode("dev", "", "", "", 0, {MakeChannel(id, topic)});
  return m;
}

}  // namespace

TEST(ChannelRegistry, DeclareAllocatesFromFirstDynamicIdempotent) {
  ChannelRegistry r("dev");
  EXPECT_EQ(r.Declare("/g/imus/0/raw", "S"), kFirstDynamic);
  EXPECT_EQ(r.Declare("/g/imus/1/raw", "S"), kFirstDynamic + 1);
  EXPECT_EQ(r.Declare("/g/imus/0/raw", "S"), kFirstDynamic);  // idempotent
  ASSERT_TRUE(r.LocalIdFor("/g/imus/0/raw").has_value());
  EXPECT_EQ(*r.LocalIdFor("/g/imus/0/raw"), kFirstDynamic);
  EXPECT_FALSE(r.LocalIdFor("/never").has_value());
}

TEST(ChannelRegistry, LearnByIdAndResolve) {
  ChannelRegistry r;
  r.Learn(MakeChannel(16, "/c/imu/0/raw"));
  const Channel* ch = r.Resolve(16);
  ASSERT_NE(ch, nullptr);
  EXPECT_EQ(ch->topic, "/c/imu/0/raw");
}

TEST(ChannelRegistry, LearnSameIdIsIdempotent) {
  ChannelRegistry r;
  r.Learn(MakeChannel(16, "/c/imu/0/raw"));
  r.Learn(MakeChannel(16, "/c/imu/0/raw"));  // re-announce
  EXPECT_EQ(r.Resolve(16)->topic, "/c/imu/0/raw");
}

TEST(ChannelRegistry, DuplicateTopicThrows) {
  ChannelRegistry r;
  r.Learn(MakeChannel(16, "/c/imu/0/raw"));
  EXPECT_THROW(r.Learn(MakeChannel(17, "/c/imu/0/raw")), DuplicateTopicError);
}

TEST(ChannelRegistry, ForgetFreesIdAndTopic) {
  ChannelRegistry r;
  r.Learn(MakeChannel(16, "/c/imu/0/raw"));
  r.Forget({16});
  EXPECT_EQ(r.Resolve(16), nullptr);
  r.Learn(MakeChannel(99, "/c/imu/0/raw"));   // topic freed: re-map under new id
  EXPECT_EQ(r.Resolve(99)->topic, "/c/imu/0/raw");
}

TEST(ChannelRegistry, AcceptLearnsAnnounceAndAbsorbs) {
  ChannelRegistry r;
  auto out = r.Accept(Announce(16, "/c/imu/0/raw"));
  EXPECT_FALSE(out.message.has_value());
  EXPECT_EQ(out.channel, nullptr);
  EXPECT_EQ(r.Resolve(16)->topic, "/c/imu/0/raw");
}

TEST(ChannelRegistry, AcceptResolvesDataAndDropsUntilKnown) {
  ChannelRegistry r;
  Message data;
  data.stream_id = 16;
  data.payload = "x";
  auto dropped = r.Accept(data);
  EXPECT_FALSE(dropped.message.has_value());
  EXPECT_EQ(r.dropped_unmapped(), 1u);

  r.Accept(Announce(16, "/c/imu/0/raw"));
  auto out = r.Accept(data);
  ASSERT_TRUE(out.message.has_value());
  ASSERT_NE(out.channel, nullptr);
  EXPECT_EQ(out.channel->topic, "/c/imu/0/raw");
}

TEST(ChannelRegistry, AcceptPassesOtherControl) {
  ChannelRegistry r;
  Message hb;
  hb.stream_id = kHeartbeat;
  hb.payload = "b";
  auto out = r.Accept(hb);
  ASSERT_TRUE(out.message.has_value());
  EXPECT_EQ(out.message->stream_id, kHeartbeat);
  EXPECT_EQ(out.channel, nullptr);
}

TEST(ChannelRegistry, EncodeDecodeRoundTrip) {
  std::vector<Channel> chans = {MakeChannel(kFirstDynamic, "/g/imus/0/raw")};
  chans[0].schema = std::string(8, '\x01');
  std::string payload = ChannelRegistry::Encode("dev", "fw", "hw", "sn", 42, chans);
  ChannelRegistry::DeviceView view;
  ASSERT_TRUE(ChannelRegistry::Decode(payload, &view));
  EXPECT_EQ(view.device_name, "dev");
  ASSERT_EQ(view.channels.size(), 1u);
  EXPECT_EQ(view.channels[0].topic, "/g/imus/0/raw");
  EXPECT_EQ(view.channels[0].schema, std::string(8, '\x01'));
}

TEST(ChannelRegistry, SelfInfoCarriesOwnAndLearned) {
  ChannelRegistry r("hub");
  r.Declare("/hub/imus/0/raw", "S");
  r.Learn(MakeChannel(r.Alloc(), "/child/imus/0/quat"));   // global id, no collision
  ChannelRegistry::DeviceView view;
  ASSERT_TRUE(ChannelRegistry::Decode(r.SelfInfo(), &view));
  EXPECT_EQ(view.channels.size(), 2u);
}
