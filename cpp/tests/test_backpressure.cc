// Backpressure / stalled-peer — an active-object endpoint owns a bounded outbox
// with a WritePolicy. A stalled peer (one that never reads) can't drain the fd,
// so the outbox fills and SHEDS frames (dropped() rises) while Send() stays
// non-blocking on the caller's thread — it never blocks behind a slow consumer.
// The positive control shows a draining peer sheds nothing.
#include <gtest/gtest.h>
#include <sys/socket.h>

#include <atomic>
#include <chrono>
#include <string>
#include <thread>

#include "visio_schema/transport/framed_fd.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/write_policy.hpp"

using namespace std::chrono_literals;
using visio_schema::transport::CloseFd;
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

Message Frame(std::size_t n) {
  Message m;
  m.stream_id = 16;
  m.payload = std::string(n, 'x');
  return m;
}

}  // namespace

TEST(Backpressure, StalledPeerShedsAndNeverBlocks) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);

  // Small bounded outbox; the peer `b` is never read, so the I/O thread cannot
  // drain `a` once the kernel buffer fills.
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
