// Frame-once fanout + per-endpoint live-rate decimation.
//
// One EncodeFramed pass serves every framed sink: the first Send fills
// Message::framed and later sinks take a refcount instead of re-running
// COBS+CRC. Decimatable messages honor the endpoint's SetLiveRateHz cap;
// everything else is untouched.
#include <gtest/gtest.h>

#include <chrono>
#include <thread>

#include "active_object_test_util.hpp"
#include "visio_schema/transport/framing.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/serial.hpp"

using visio_schema::transport::EncodeFramed;
using visio_schema::transport::MakeFdPair;
using visio_schema::transport::SerialEndpoint;
using visio_schema::transport::test::InboundCollector;
using visio_schema::wire::Message;

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

TEST(LiveRateDecimation, CapsDecimatableMessagesPerStream) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  // 1 Hz: the min gap (1 s) cannot be straddled by the microsecond burst
  // below even on a badly stalled CI machine — the exact-count assertion
  // stays deterministic.
  tx.SetLiveRateHz(1);

  // A burst far faster than 1 Hz: only the first of each stream passes.
  for (int i = 0; i < 10; ++i) {
    Message m;
    m.stream_id = 20;
    m.payload = "quat";
    m.decimatable = true;
    tx.Send(m);
  }
  Message other;
  other.stream_id = 21;  // distinct stream: rate cap is per-stream
  other.payload = "quat2";
  other.decimatable = true;
  tx.Send(other);

  ASSERT_GE(rxc.wait_for(2), 2u);
  // Give any stragglers time to (wrongly) arrive before counting.
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  EXPECT_EQ(rxc.messages().size(), 2u);

  tx.Stop();
  rx.Stop();
}

TEST(LiveRateDecimation, ZeroRateAndPlainMessagesPassUntouched) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  // rate 0 (default): decimatable passes at full rate.
  for (int i = 0; i < 5; ++i) {
    Message m;
    m.stream_id = 22;
    m.payload = "full";
    m.decimatable = true;
    tx.Send(m);
  }
  // Rate set, but plain (non-decimatable) messages are never rate-limited.
  tx.SetLiveRateHz(1);
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
