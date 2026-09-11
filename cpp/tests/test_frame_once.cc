// Frame-once fanout + per-endpoint stream-policy decimation.
//
// One EncodeFramed pass serves every framed sink: the first Send fills
// Message::framed and later sinks take a refcount instead of re-running
// COBS+CRC. A stream the client capped is decimated per stream; a stream it
// named no rule for is untouched.
#include <gtest/gtest.h>

#include <sys/socket.h>

#include <chrono>
#include <cstdint>
#include <initializer_list>
#include <memory>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "active_object_test_util.hpp"
#include "visio_schema/transport/framing.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/serial.hpp"
#include "visio_schema/transport/stream_policy.hpp"
#include "visio_schema/wire/time.hpp"

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

// ── SendBatch: one wake, so the outbox actually has something to coalesce ────

namespace {

// A SEQPACKET pair makes every write() a datagram the far side reads whole, so
// counting recv()s counts WRITES. That is the only thing SendBatch changes, and
// counting it is the only way to see it: the decoded messages are identical
// either way.
std::pair<int, int> MakeSeqPacketPair() {
  int sv[2] = {-1, -1};
  EXPECT_EQ(::socketpair(AF_UNIX, SOCK_SEQPACKET, 0, sv), 0);
  return {sv[0], sv[1]};
}

// Read datagrams until `quiet_ms` passes with none. Returns their count.
int CountWrites(int fd, int quiet_ms = 200) {
  int writes = 0;
  const auto deadline = [&] {
    return std::chrono::steady_clock::now() +
           std::chrono::milliseconds(quiet_ms);
  };
  auto until = deadline();
  std::uint8_t buf[65536];
  while (std::chrono::steady_clock::now() < until) {
    const ssize_t r = ::recv(fd, buf, sizeof(buf), MSG_DONTWAIT);
    if (r > 0) {
      ++writes;
      until = deadline();
    } else {
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
  }
  return writes;
}

// Capture stamps 4 ms apart, off a nonzero base: a stamp of exactly zero means
// "the producer set none" and takes the send-time fallback (see PassesRateGate),
// which is a different path from the one these tests are about. Nothing on the
// bus can hit it — Bus::StampHeader always writes a time — but a hand-built
// Message can, so the fixtures stay clear of it.
constexpr std::int64_t kBaseNs = 100'000'000;  // 100 ms

// One message stamped `ms` past the base, for the gate tests.
Message At(std::uint32_t stream_id, std::int64_t ms, const char* tag) {
  Message m;
  m.stream_id = stream_id;
  m.payload = tag;
  visio_schema::SetTimestampNs(&m.timestamp, kBaseNs + ms * 1'000'000);
  return m;
}

Message Quat(std::uint32_t stream_id, int i) {
  Message m;
  m.stream_id = stream_id;
  m.payload = std::to_string(i);
  m.decimatable = true;
  visio_schema::SetTimestampNs(
      &m.timestamp, kBaseNs + static_cast<std::int64_t>(i) * 4'000'000);
  return m;
}

}  // namespace

// The headline: N messages leave as ONE write, where N separate Sends leave as
// N. This is the whole reason SendBatch exists — a small frame per write is a
// small PACKET per write on a TCP leg, and the per-packet cost is what saturates
// a single-core device long before the bandwidth does.
TEST(SendBatch, GroupLeavesAsOneWriteWhereSeparateSendsLeaveAsMany) {
  constexpr int kN = 8;
  {
    auto [a, b] = MakeSeqPacketPair();
    SerialEndpoint tx(a);
    tx.Start(nullptr, nullptr);
    std::vector<Message> batch;
    for (int i = 0; i < kN; ++i) batch.push_back(Quat(50, i));
    tx.SendBatch(batch.data(), batch.size());
    // Fewer writes than messages is the contract; one is the normal result. Not
    // EQ(1): the I/O thread's own idle tick may legitimately split a group, and
    // SendBatch's comment says so — asserting 1 would be asserting a coincidence.
    EXPECT_LT(CountWrites(b), kN);
    tx.Stop();
    visio_schema::transport::CloseFd(b);
  }
  {
    auto [a, b] = MakeSeqPacketPair();
    SerialEndpoint tx(a);
    tx.Start(nullptr, nullptr);
    // Same messages one at a time: each wake drains a single frame, so BatchAll
    // has nothing to fold and every message becomes its own write.
    for (int i = 0; i < kN; ++i) {
      Message m = Quat(50, i);
      tx.Send(m);
      // Let the leg thread actually drain before the next one, which is what a
      // real producer's inter-sample gap does.
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    EXPECT_EQ(CountWrites(b), kN);
    tx.Stop();
    visio_schema::transport::CloseFd(b);
  }
}

TEST(SendBatch, DeliversEveryMessageInOrder) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);

  std::vector<Message> batch;
  for (int i = 0; i < 6; ++i) batch.push_back(Quat(51, i));
  tx.SendBatch(batch.data(), batch.size());

  ASSERT_GE(rxc.wait_for(6), 6u);
  for (int i = 0; i < 6; ++i) {
    EXPECT_EQ(rxc.messages()[i].payload, std::to_string(i));
  }
  tx.Stop();
  rx.Stop();
}

// A batch is N independent messages that happen to travel together, not one
// unit that passes or fails as a whole: every gate still runs per message.
TEST(SendBatch, AppliesEveryGatePerMessage) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  tx.SetStreamPolicy(Policy({{52, StreamRule{true, 0}},              // dropped
                             {53, StreamRule{false, 1'000'000}}}));  // 1 Hz

  std::vector<Message> batch;
  for (int i = 0; i < 3; ++i) batch.push_back(Quat(52, i));  // all dropped
  for (int i = 0; i < 3; ++i) batch.push_back(Quat(53, i));  // first only
  for (int i = 0; i < 2; ++i) batch.push_back(Quat(54, i));  // uncapped: both

  tx.SendBatch(batch.data(), batch.size());
  ASSERT_GE(rxc.wait_for(3), 3u);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  EXPECT_EQ(rxc.messages().size(), 3u);

  tx.Stop();
  rx.Stop();
}

// ── The rate gate: capture time, and a grid ─────────────────────────────────

// The regression this whole change exists for. Eleven messages handed to Send
// back-to-back — so every ARRIVAL time is the same microsecond — carrying
// capture stamps 4 ms apart, under a 100 Hz cap.
//
// Timed at arrival, one message survives the burst and the delivered rate is
// whatever the producer's flush rate happens to be. Timed at CAPTURE, the cap
// means what it says: the stamps span 40 ms, so 100 Hz passes five of them, at
// the closest the source's own 4 ms quantisation allows to a 10 ms grid.
TEST(StreamPolicyDecimation, CapsOnCaptureTimeNotArrivalTime) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  tx.SetStreamPolicy(Policy({{55, StreamRule{false, 10'000}}}));  // 100 Hz

  for (int i = 0; i < 11; ++i) {
    Message m = Quat(55, i);  // capture t = i * 4 ms
    tx.Send(m);
  }

  ASSERT_GE(rxc.wait_for(5), 5u);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  ASSERT_EQ(rxc.messages().size(), 5u);
  // t = 0, 12, 20, 32, 40 ms — gaps of 12/8/12/8, averaging the 10 ms asked
  // for. A source quantised to 4 ms cannot land on a 10 ms grid exactly; what
  // matters is that it neither drifts nor collapses.
  const std::vector<std::string> want = {"0", "3", "5", "8", "10"};
  for (std::size_t i = 0; i < want.size(); ++i) {
    EXPECT_EQ(rxc.messages()[i].payload, want[i]) << "at index " << i;
  }
  tx.Stop();
  rx.Stop();
}

// After a silence the grid snaps to the next slot rather than paying out the
// slots it missed — a stream that stops for five seconds must not come back as
// a five-hundred-message catch-up burst.
TEST(StreamPolicyDecimation, SnapsForwardAfterSilenceInsteadOfCatchingUp) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  tx.SetStreamPolicy(Policy({{56, StreamRule{false, 10'000}}}));  // 100 Hz

  tx.Send(At(56, 0, "first"));
  tx.Send(At(56, 5'000, "after-silence"));  // 5 s later: passes
  tx.Send(At(56, 5'005, "too-soon"));       // 5 ms on: still inside the gap
  tx.Send(At(56, 5'010, "next-slot"));      // exactly one gap on: passes

  ASSERT_GE(rxc.wait_for(3), 3u);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  ASSERT_EQ(rxc.messages().size(), 3u);
  EXPECT_EQ(rxc.messages()[0].payload, "first");
  EXPECT_EQ(rxc.messages()[1].payload, "after-silence");
  EXPECT_EQ(rxc.messages()[2].payload, "next-slot");
  tx.Stop();
  rx.Stop();
}

// A grid is an absolute deadline, so a stream whose clock jumps BACKWARDS would
// be muted until real time caught up — forever, if the epoch changed. Re-seed on
// a big step back, but keep shedding ordinary out-of-order jitter.
TEST(StreamPolicyDecimation, ReSeedsOnABackwardsEpochButNotOnJitter) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  tx.SetStreamPolicy(Policy({{57, StreamRule{false, 10'000}}}));  // 100 Hz

  const auto send_at_ms = [&](std::int64_t ms, const char* tag) {
    Message m;
    m.stream_id = 57;
    m.payload = tag;
    visio_schema::SetTimestampNs(&m.timestamp, ms * 1'000'000);
    tx.Send(m);
  };
  tx.Send(At(57, 10'000, "seed"));        // grid -> 10,010 ms
  tx.Send(At(57, 10'008, "jitter"));      // 2 ms behind the grid: shed, not re-seed
  tx.Send(At(57, 8'000, "new-epoch"));    // >1 s behind: the clock changed
  tx.Send(At(57, 8'005, "same-epoch"));   // and the new grid is honoured from there

  ASSERT_GE(rxc.wait_for(2), 2u);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  ASSERT_EQ(rxc.messages().size(), 2u);
  EXPECT_EQ(rxc.messages()[0].payload, "seed");
  EXPECT_EQ(rxc.messages()[1].payload, "new-epoch");
  tx.Stop();
  rx.Stop();
}

// A CHANGED period drops the phase held under the old one: a grid still holding
// the old gap's deadline would mute the stream until that stale deadline passed.
TEST(StreamPolicyDecimation, SetStreamPolicyClearsThePhaseWhenThePeriodChanges) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);

  tx.SetStreamPolicy(Policy({{58, StreamRule{false, 1'000'000}}}));  // 1 Hz
  tx.Send(At(58, 0, "seed"));    // grid -> 1000 ms
  tx.Send(At(58, 1, "shed"));    // inside the 1 Hz gap
  tx.SetStreamPolicy(Policy({{58, StreamRule{false, 10'000}}}));  // now 100 Hz
  tx.Send(At(58, 2, "after-repolicy"));  // would still be shed under the old grid

  ASSERT_GE(rxc.wait_for(2), 2u);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  ASSERT_EQ(rxc.messages().size(), 2u);
  EXPECT_EQ(rxc.messages()[0].payload, "seed");
  EXPECT_EQ(rxc.messages()[1].payload, "after-repolicy");
  tx.Stop();
  rx.Stop();
}

// ...but re-applying the SAME period must not. StreamPolicyService re-resolves
// every link's rules on any channel change, so an unchanged table lands here
// repeatedly; restarting the phase on those would let one extra message through
// each time and put the delivered rate OVER the cap. Measured on hardware before
// this was fixed: a 60 Hz cap delivering ~70 Hz.
TEST(StreamPolicyDecimation, ReApplyingTheSamePeriodKeepsThePhase) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);

  const auto cap100 = [&] {
    tx.SetStreamPolicy(Policy({{59, StreamRule{false, 10'000}}}));
  };
  cap100();
  tx.Send(At(59, 0, "seed"));   // grid -> 10 ms
  cap100();                // identical table, as a channel change would re-send
  tx.Send(At(59, 2, "shed"));   // still inside the gap: the phase survived
  cap100();
  tx.Send(At(59, 4, "shed2"));
  tx.Send(At(59, 10, "next-slot"));

  ASSERT_GE(rxc.wait_for(2), 2u);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  ASSERT_EQ(rxc.messages().size(), 2u);
  EXPECT_EQ(rxc.messages()[0].payload, "seed");
  EXPECT_EQ(rxc.messages()[1].payload, "next-slot");
  tx.Stop();
  rx.Stop();
}

// The rate cap is per-SINK, and a batch does not change that: the same group
// handed to a capped leg and to an uncapped one is thinned on the first and
// arrives whole on the second. That is the shape of a device streaming a thinned
// preview while recording losslessly, and it is exactly what must not regress
// when a producer starts publishing in groups.
TEST(SendBatch, CapsOneSinkWithoutThinningAnother) {
  auto [a1, b1] = MakeFdPair();
  auto [a2, b2] = MakeFdPair();
  SerialEndpoint capped(a1), lossless(a2), rx1(b1), rx2(b2);
  InboundCollector c1, c2;
  rx1.Start(c1.fn(), c1.on_closed());
  rx2.Start(c2.fn(), c2.on_closed());
  capped.Start(nullptr, nullptr);
  lossless.Start(nullptr, nullptr);
  // Only the preview leg gets a policy. A recording sink never calls
  // SetStreamPolicy at all, which is why it keeps everything (endpoint.hpp).
  capped.SetStreamPolicy(Policy({{60, StreamRule{false, 10'000}}}));  // 100 Hz

  std::vector<Message> batch;
  for (int i = 0; i < 11; ++i) batch.push_back(Quat(60, i));  // 4 ms apart
  capped.SendBatch(batch.data(), batch.size());
  lossless.SendBatch(batch.data(), batch.size());

  ASSERT_GE(c2.wait_for(11), 11u);
  ASSERT_GE(c1.wait_for(5), 5u);
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  EXPECT_EQ(c1.messages().size(), 5u);   // 0, 12, 20, 32, 40 ms
  EXPECT_EQ(c2.messages().size(), 11u);  // every sample

  capped.Stop();
  lossless.Stop();
  rx1.Stop();
  rx2.Stop();
}

// A stream that mixes capture-stamped and unstamped messages defeats the cap
// entirely — the two clocks have unrelated origins, so one message steps the grid
// far forward and the next trips the epoch re-seed, and everything passes. It is a
// producer bug and cannot be fixed from inside the gate, but it buys exactly the
// saturation the gate exists to prevent, so it has to be loud.
TEST(StreamPolicyDecimation, ComplainsOnceWhenAStreamMixesStampedAndUnstamped) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);
  tx.SetStreamPolicy(Policy({{61, StreamRule{false, 10'000}}}));  // 100 Hz

  testing::internal::CaptureStderr();
  for (int i = 0; i < 6; ++i) {
    tx.Send(At(61, i * 4, "stamped"));
    Message bare;                  // no capture time at all
    bare.stream_id = 61;
    bare.payload = "unstamped";
    tx.Send(bare);
  }
  const std::string err = testing::internal::GetCapturedStderr();

  // Once, not once per message.
  const std::string needle = "mixes capture-stamped and unstamped";
  ASSERT_NE(err.find(needle), std::string::npos) << "no warning: [" << err << "]";
  EXPECT_EQ(err.find(needle, err.find(needle) + 1), std::string::npos)
      << "warned more than once";

  tx.Stop();
  rx.Stop();
}
