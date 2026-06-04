// Backpressure / stalled-peer — a reactor SerialEndpoint never blocks the bus
// thread and never throws when the consumer stops reading. Writes go through a
// bounded FramedOutbox (WritePolicy), so a peer that fills the kernel send buffer
// makes the outbox shed (drop-oldest) rather than block or disconnect. The
// positive control shows a draining peer keeps up with no shedding-induced error.
//
// This replaces the old write-timeout contract (where a stalled write tripped a
// poll(POLLOUT) timeout and surfaced EndpointClosed): backpressure is now an
// outbox property, not a per-write blocking timeout. See umi_channel.hpp.
#include <gtest/gtest.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <string>
#include <thread>

#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/serial.hpp"
#include "visio_schema/transport/write_policy.hpp"

using visio_schema::transport::MakeFdLink;
using visio_schema::transport::SerialEndpoint;
using visio_schema::transport::WritePolicy;
using visio_schema::wire::Message;

namespace {

// A connected stream socket pair with small buffers so a non-draining peer fills
// fast. Returns {writer_fd, peer_fd}.
std::pair<int, int> SmallSocketPair() {
  int sv[2] = {-1, -1};
  EXPECT_EQ(::socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
  int sz = 8192;
  ::setsockopt(sv[0], SOL_SOCKET, SO_SNDBUF, &sz, sizeof(sz));
  ::setsockopt(sv[1], SOL_SOCKET, SO_RCVBUF, &sz, sizeof(sz));
  return {sv[0], sv[1]};
}

}  // namespace

TEST(Backpressure, StalledPeerSheddsWithoutBlockingOrThrowing) {
  auto [wfd, peer] = SmallSocketPair();
  // drop-oldest with a small depth: a peer that never reads makes the outbox
  // shed once both the kernel send buffer and the queue are full.
  SerialEndpoint tx(MakeFdLink(wfd, /*set_raw=*/false),
                    WritePolicy::drop_oldest(/*depth=*/8));
  Message m;
  m.stream_id = 16;
  m.payload = std::string(2048, 'x');

  // Far more than the 8 KB socket buffer + 8-frame queue can hold. Must neither
  // block (the fd is forced non-blocking) nor throw (it sheds per policy).
  EXPECT_NO_THROW({
    for (int i = 0; i < 1000; ++i) tx.Write(m);
  });
  // The outbox stays bounded by the policy (<=8 queued + 1 in-flight frame),
  // independent of how many writes were offered: 1000 un-shed 2 KB frames would
  // be ~2 MB, so a tens-of-KB ceiling proves drop-oldest held.
  EXPECT_LT(tx.pending_bytes(), 32u * 1024u);
  EXPECT_TRUE(tx.link_up());  // a stalled (not broken) peer keeps the link up
  ::close(peer);
}

TEST(Backpressure, DrainedPeerKeepsUpAndDelivers) {
  int sv[2] = {-1, -1};
  ASSERT_EQ(::socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);  // default buffers
  const int wfd = sv[0], peer = sv[1];

  std::atomic<std::size_t> total{0};
  std::thread reader([peer, &total] {
    char buf[8192];
    ssize_t n;
    while ((n = ::read(peer, buf, sizeof(buf))) > 0) total += static_cast<std::size_t>(n);
  });

  const int N = 200;
  const std::size_t kPayload = 16384;
  {
    SerialEndpoint tx(MakeFdLink(wfd, /*set_raw=*/false),
                      WritePolicy::drop_oldest(/*depth=*/2048));
    Message m;
    m.stream_id = 16;
    m.payload = std::string(kPayload, 'y');
    EXPECT_NO_THROW({
      for (int i = 0; i < N; ++i) tx.Write(m);
    });
    EXPECT_TRUE(tx.link_up());
    // Flush anything still buffered in the outbox to the socket (standalone — no
    // bus POLLOUT pump). The peer is draining, so WriteSome keeps making room.
    for (int i = 0; i < 2000 && tx.pending_bytes() > 0; ++i) {
      tx.OnWritable();
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    EXPECT_EQ(tx.pending_bytes(), 0u);
  }  // tx closes wfd → reader drains the socket, then sees EOF

  reader.join();
  ::close(peer);
  // A draining peer must actually RECEIVE the stream (not silently shed it): the
  // bytes read exceed the raw payload total (framing adds header + COBS overhead).
  EXPECT_GT(total.load(), static_cast<std::size_t>(N) * kPayload);
}
