// io tests — SerialEndpoint over a pty pair round-trips a framed message, and a
// broken link surfaces as EndpointClosed (endpoints never self-reconnect).
#include <gtest/gtest.h>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/serial.hpp"

using visio_schema::transport::EndpointClosed;
using visio_schema::transport::MakeFdLinkPair;
using visio_schema::transport::SerialEndpoint;
using visio_schema::wire::Message;

TEST(Io, SerialRoundTripsAFrame) {
  auto [a, b] = MakeFdLinkPair();
  SerialEndpoint tx(a), rx(b);
  Message m;
  m.stream_id = 16;
  m.payload = "hello";
  tx.Write(m);

  std::vector<Message> got;
  for (int i = 0; i < 100 && got.empty(); ++i) got = rx.TryRead();
  ASSERT_EQ(got.size(), 1u);
  EXPECT_EQ(got[0].stream_id, 16u);
  EXPECT_EQ(got[0].payload, "hello");
}

TEST(Io, EofRaisesEndpointClosed) {
  auto [a, b] = MakeFdLinkPair();
  SerialEndpoint rx(b);
  a->Close();   // peer hangs up
  EXPECT_THROW({ (void)rx.TryRead(); }, EndpointClosed);
}

// A reactor sink never blocks or throws on a broken link: Write() enqueues into
// the bounded outbox and the best-effort drain finds the link dead, dropping it.
// (For a fixed link the break also surfaces on the read path, where the bus
// detaches it; here we assert the write side neither throws nor hangs.)
TEST(Io, BrokenWriteSheddsWithoutThrowing) {
  auto [a, b] = MakeFdLinkPair();
  SerialEndpoint tx(a);
  a->Close();   // local link gone
  Message m;
  m.stream_id = 16;
  m.payload = "x";
  EXPECT_NO_THROW(tx.Write(m));
  EXPECT_FALSE(tx.link_up());  // dead fixed link is dropped, not reopened
}
