// Frame-once fanout + per-endpoint stream-policy decimation.
//
// One EncodeFramed pass serves every framed sink: the first Send fills
// Message::framed and later sinks take a refcount instead of re-running
// COBS+CRC. A stream the client capped is decimated per stream; a stream it
// named no rule for is untouched.
#include <gtest/gtest.h>

#include <chrono>
#include <cstdint>
#include <initializer_list>
#include <memory>
#include <thread>
#include <utility>

#include "active_object_test_util.hpp"
#include "visio_schema/transport/framing.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/serial.hpp"
#include "visio_schema/transport/stream_policy.hpp"

using visio_schema::transport::EncodeFramed;
using visio_schema::transport::MakeFdPair;
using visio_schema::transport::ResolvedStreamPolicy;
using visio_schema::transport::SerialEndpoint;
using visio_schema::transport::StreamRule;
using visio_schema::transport::test::InboundCollector;
using visio_schema::wire::Message;

namespace {

// The already-resolved table an endpoint is handed — the bus does the topic
// glob -> stream_id step, which these transport tests deliberately skip.
std::shared_ptr<const ResolvedStreamPolicy> Policy(
    std::initializer_list<std::pair<const std::uint32_t, StreamRule>> rules) {
  return std::make_shared<const ResolvedStreamPolicy>(rules);
}

}  // namespace

TEST(FrameOnce, SendFillsTheCacheWithTheExactWireBytes) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);

  Message m;
  m.stream_id = 16;
  m.payload = "cached-once";
  ASSERT_FALSE(m.framed);
  tx.Send(m);
  // Send framed it exactly once, and the cache IS the wire encoding.
  ASSERT_TRUE(m.framed);
  EXPECT_EQ(*m.framed, EncodeFramed(m));

  ASSERT_GE(rxc.wait_for(1), 1u);
  EXPECT_EQ(rxc.messages()[0].payload, "cached-once");
  tx.Stop();
  rx.Stop();
}

TEST(FrameOnce, SecondSinkReusesTheCacheAndDeliversIdenticalBytes) {
  auto [a1, b1] = MakeFdPair();
  auto [a2, b2] = MakeFdPair();
  SerialEndpoint tx1(a1), tx2(a2), rx1(b1), rx2(b2);
  InboundCollector c1, c2;
  rx1.Start(c1.fn(), c1.on_closed());
  rx2.Start(c2.fn(), c2.on_closed());
  tx1.Start(nullptr, nullptr);
  tx2.Start(nullptr, nullptr);

  Message m;
  m.stream_id = 17;
  m.payload = "fanout";
  tx1.Send(m);
  const auto* first_cache = m.framed.get();
  tx2.Send(m);  // must reuse, not re-encode
  EXPECT_EQ(m.framed.get(), first_cache);

  ASSERT_GE(c1.wait_for(1), 1u);
  ASSERT_GE(c2.wait_for(1), 1u);
  EXPECT_EQ(c1.messages()[0].payload, c2.messages()[0].payload);
  EXPECT_EQ(c1.messages()[0].stream_id, c2.messages()[0].stream_id);

  tx1.Stop();
  tx2.Stop();
  rx1.Stop();
  rx2.Stop();
}

TEST(StreamPolicyDecimation, CapsEachCappedStreamSeparately) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  // 1 Hz: the min gap (1 s) cannot be straddled by the microsecond burst
  // below even on a badly stalled CI machine — the exact-count assertion
  // stays deterministic.
  constexpr std::int64_t kOneSecond = 1'000'000;
  tx.SetStreamPolicy(Policy({{20, StreamRule{false, kOneSecond}},
                             {21, StreamRule{false, kOneSecond}}}));

  // A burst far faster than 1 Hz: only the first of each stream passes.
  for (int i = 0; i < 10; ++i) {
    Message m;
    m.stream_id = 20;
    m.payload = "quat";
    m.decimatable = true;
    tx.Send(m);
  }
  Message other;
  other.stream_id = 21;  // distinct stream: each carries its own clock. Also
  other.payload = "raw";  // FLAGLESS — a cap is no longer tied to `decimatable`,
  tx.Send(other);         // which is what lets the raw IMU bundles be thinned.

  ASSERT_GE(rxc.wait_for(2), 2u);
  // Give any stragglers time to (wrongly) arrive before counting.
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  EXPECT_EQ(rxc.messages().size(), 2u);

  tx.Stop();
  rx.Stop();
}

TEST(StreamPolicyDecimation, DropsWhatTheRuleDrops) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  tx.SetStreamPolicy(Policy({{30, StreamRule{true, 0}}}));

  for (int i = 0; i < 5; ++i) {
    Message m;
    m.stream_id = 30;  // e.g. the raw IMU bundles a preview never reads
    m.payload = "raw";
    tx.Send(m);
  }
  Message kept;
  kept.stream_id = 31;
  kept.payload = "kept";
  tx.Send(kept);

  ASSERT_GE(rxc.wait_for(1), 1u);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  ASSERT_EQ(rxc.messages().size(), 1u);
  EXPECT_EQ(rxc.messages()[0].payload, "kept");

  tx.Stop();
  rx.Stop();
}

// Video is keep-or-drop: a cap on a bulk stream must be ignored, because
// shedding P-frames costs the decoder its reference chain for a whole GOP.
TEST(StreamPolicyDecimation, RateCapIsIgnoredForBulkVideo) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  tx.SetStreamPolicy(Policy({{40, StreamRule{false, 1'000'000}}}));

  for (int i = 0; i < 5; ++i) {
    Message m;
    m.stream_id = 40;
    m.payload = "frame";
    m.bulk = true;
    tx.Send(m);
  }
  ASSERT_GE(rxc.wait_for(5), 5u);
  tx.Stop();
  rx.Stop();
}

TEST(StreamPolicyDecimation, UnmatchedStreamsAndZeroRatePassUntouched) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  // No policy at all (a fresh connection): everything at full rate, which is
  // what keeps a client recording from the live stream lossless.
  for (int i = 0; i < 5; ++i) {
    Message m;
    m.stream_id = 22;
    m.payload = "full";
    m.decimatable = true;
    tx.Send(m);
  }
  // A policy that caps ONE stream leaves every stream it does not name alone —
  // absent from the table means keep, not drop.
  tx.SetStreamPolicy(Policy({{99, StreamRule{false, 1'000'000}}}));
  for (int i = 0; i < 5; ++i) {
    Message m;
    m.stream_id = 23;
    m.payload = "ctrl";
    tx.Send(m);
  }
  ASSERT_GE(rxc.wait_for(10), 10u);
  tx.Stop();
  rx.Stop();
}
