// Backpressure / stalled-peer — an active-object endpoint owns a bounded outbox
// with a WritePolicy. A stalled peer (one that never reads) can't drain the fd,
// so the outbox fills and SHEDS frames (dropped() rises) while Send() stays
// non-blocking on the caller's thread — it never blocks behind a slow consumer.
// The positive control shows a draining peer sheds nothing.
#include <gtest/gtest.h>
#include <sys/socket.h>

#include <atomic>
#include <chrono>
#include <mutex>
#include <string>
#include <vector>
#include <thread>

#include "visio_schema/transport/framed_fd.hpp"
#include "visio_schema/transport/framing.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/write_policy.hpp"

using namespace std::chrono_literals;
using visio_schema::transport::CloseFd;
using visio_schema::transport::ExtractFrames;
using visio_schema::transport::FramedFdEndpoint;
using visio_schema::transport::MakeFdPair;
using visio_schema::transport::ReadSome;
using visio_schema::transport::WritePolicy;
using visio_schema::wire::Message;

namespace {

// Shrink both ends' socket buffers so a non-draining peer fills fast and the
// outbox starts shedding within a small flood.
void ShrinkBuffers(int a, int b) {
  int sz = 4096;
  ::setsockopt(a, SOL_SOCKET, SO_SNDBUF, &sz, sizeof(sz));
  ::setsockopt(b, SOL_SOCKET, SO_RCVBUF, &sz, sizeof(sz));
}

// Mark the frame `bulk` so Send() routes it to the outbox whose depth THIS
// endpoint's WritePolicy configures (`outbox_`). Non-bulk frames go to the
// separate control outbox, which is hardwired to drop_oldest(512) and ignores
// the constructor policy — so a non-bulk flood tests a queue the test never
// sized, making the shed/no-shed outcome a race (flaky, esp. on macOS where
// small default AF_UNIX socket buffers stall the drainer).
Message Frame(std::size_t n) {
  Message m;
  m.stream_id = 16;
  m.payload = std::string(n, 'x');
  m.bulk = true;
  return m;
}

}  // namespace

TEST(Backpressure, StalledPeerShedsAndNeverBlocks) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);

  // Small bounded (bulk) outbox; the peer `b` is never read, so the I/O thread
  // cannot drain `a` once the kernel buffer fills.
  FramedFdEndpoint tx(a, WritePolicy::drop_oldest(/*depth=*/8));
  tx.Start({}, {});  // write-only sink: no inbound / on_closed needed

  // Flood. Send() must never block the caller even though nothing drains.
  const Message m = Frame(512);
  for (int i = 0; i < 5000; ++i) tx.Send(m);

  // Shedding is applied at enqueue once the queue hits max_depth; give the I/O
  // thread a beat to fill the kernel buffer + back up the queue, then assert.
  bool shed = false;
  for (int i = 0; i < 200 && !shed; ++i) {
    if (tx.dropped() > 0) {
      shed = true;
    } else {
      tx.Send(m);
      std::this_thread::sleep_for(1ms);
    }
  }
  EXPECT_TRUE(shed) << "a stalled peer should make the bounded outbox shed frames";
  // Shedding must be substantial — most of the 5000-frame flood, since the
  // depth-8 outbox can only hold a handful. A regression that left the queue
  // effectively unbounded (the bug a bounded outbox prevents) would shed ~none.
  EXPECT_GT(tx.dropped(), 100u);

  tx.Stop();
  CloseFd(b);
}

TEST(Backpressure, DrainingPeerShedsNothing) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);

  std::atomic<bool> stop{false};
  std::thread reader([b = b, &stop] {
    std::uint8_t buf[4096];
    while (!stop.load()) {
      long n = ReadSome(b, buf, sizeof(buf));
      if (n < 0) break;            // EOF / dead
      if (n == 0) std::this_thread::sleep_for(1ms);  // would-block: nothing yet
    }
  });

  // Depth-1024 (bulk) outbox holds the whole 1000-frame flood without shedding,
  // even if the drainer never runs — so "sheds nothing" is deterministic, not a
  // race against the peer/OS.
  FramedFdEndpoint tx(a, WritePolicy::drop_oldest(1024));
  tx.Start({}, {});
  const Message m = Frame(512);
  for (int i = 0; i < 1000; ++i) tx.Send(m);

  // With the peer keeping up, the outbox drains to empty and nothing is shed.
  bool drained = false;
  for (int i = 0; i < 500 && !drained; ++i) {
    if (tx.pending_bytes() == 0) drained = true;
    else std::this_thread::sleep_for(1ms);
  }
  EXPECT_TRUE(drained) << "outbox should fully drain to a keeping-up peer";
  EXPECT_EQ(tx.dropped(), 0u);

  tx.Stop();
  stop.store(true);
  CloseFd(b);
  reader.join();
}

// A draining-but-slow peer forces WriteSome to return EAGAIN, which is the case
// that used to throttle the leg: Pump() called Drain() once per poll wakeup and
// a OneAtATime outbox promotes exactly ONE frame per Drain, so the endpoint could
// only put one frame on the wire per POLLOUT. That is invisible on a fast link
// (writes never block, so poll returns immediately and the loop spins) but caps
// throughput on a real one — on the ego it left the kernel send queue empty while
// the viewer starved and its decoder lost sync. Pump() now keeps draining until
// the link stalls.
//
// The throughput half of that is NOT assertable here: over a socketpair poll()
// reports writable immediately, so even one-frame-per-wakeup keeps up and this
// test passes with or without the fix. It needs POLLOUT to be rate-limited by a
// real netdev, so it is verified on hardware instead (the app-side [visio-fps]
// meter: rx and rn both ~30/s, feedGapMax near one frame interval).
//
// Pinned here by the two properties that can actually break: EVERYTHING sent
// arrives, and it arrives INTACT — the drain loop re-picks between the control
// and bulk outboxes each iteration, so a boundary bug would splice two frames
// together and desync the reader.
TEST(Backpressure, DrainsBurstThroughAStallingLinkWithoutSplicingFrames) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);  // small buffers => writes hit EAGAIN mid-burst

  std::vector<std::uint8_t> rx;
  std::mutex rx_mu;
  std::atomic<bool> stop{false};
  std::thread reader([b = b, &rx, &rx_mu, &stop] {
    std::uint8_t buf[1024];
    while (!stop.load()) {
      const long n = ReadSome(b, buf, sizeof(buf));
      if (n < 0) break;
      if (n == 0) {
        std::this_thread::sleep_for(1ms);
        continue;
      }
      std::lock_guard<std::mutex> lk(rx_mu);
      rx.insert(rx.end(), buf, buf + n);
    }
  });

  // OneAtATime is the mode the device's TCP leg uses; the queue is deep enough
  // that nothing sheds, so a missing frame means it was never drained.
  WritePolicy policy = WritePolicy::drop_oldest(4096);
  policy.drain = WritePolicy::DrainMode::OneAtATime;
  FramedFdEndpoint tx(a, policy);
  tx.Start({}, {});

  constexpr int kFrames = 300;
  for (int i = 0; i < kFrames; ++i) {
    Message bulk = Frame(512);
    bulk.seq = static_cast<std::uint32_t>(i);
    tx.Send(bulk);
    if (i % 10 == 0) {  // interleave control traffic across the drain loop
      Message ctrl;
      ctrl.stream_id = 4;
      ctrl.payload = "ctrl";
      ctrl.bulk = false;
      tx.Send(ctrl);
    }
  }

  bool drained = false;
  for (int i = 0; i < 3000 && !drained; ++i) {
    if (tx.pending_bytes() == 0) drained = true;
    else std::this_thread::sleep_for(1ms);
  }
  EXPECT_TRUE(drained) << "the outbox must drain through a link that EAGAINs";
  EXPECT_EQ(tx.dropped(), 0u);

  // pending_bytes()==0 means the endpoint handed everything to the kernel, not
  // that the peer has read it — let the reader catch up before tearing it down,
  // or the tail of the burst is lost to the test rather than to the code.
  for (int i = 0, stable = 0; i < 500 && stable < 20; ++i) {
    std::size_t before;
    {
      std::lock_guard<std::mutex> lk(rx_mu);
      before = rx.size();
    }
    std::this_thread::sleep_for(1ms);
    std::lock_guard<std::mutex> lk(rx_mu);
    stable = (rx.size() == before) ? stable + 1 : 0;
  }

  tx.Stop();
  stop.store(true);
  CloseFd(b);
  reader.join();

  std::lock_guard<std::mutex> lk(rx_mu);
  const std::vector<Message> got = ExtractFrames(rx);
  int bulk_seen = 0;
  int ctrl_seen = 0;
  for (const Message& m : got) {
    if (m.stream_id == 4) {
      ++ctrl_seen;
      EXPECT_EQ(m.payload, "ctrl");
    } else {
      // Intact payload == the frames were never spliced into each other.
      EXPECT_EQ(m.payload.size(), 512u);
      EXPECT_EQ(m.seq, static_cast<std::uint32_t>(bulk_seen)) << "bulk order";
      ++bulk_seen;
    }
  }
  EXPECT_EQ(bulk_seen, kFrames);
  EXPECT_EQ(ctrl_seen, (kFrames + 9) / 10);
}
