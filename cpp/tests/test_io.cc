// io tests — SerialEndpoint over an fd pair round-trips a framed message, and a
// broken link surfaces via callbacks (endpoints are active objects that own their
// own I/O thread; they never self-reconnect on a fixed fd). Tests close the PEER
// fd (the endpoint owns + closes its own) to simulate a hangup.
#include <gtest/gtest.h>

#include <chrono>
#include <thread>

#include "active_object_test_util.hpp"
#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/serial.hpp"

using visio_schema::transport::CloseFd;
using visio_schema::transport::MakeFdPair;
using visio_schema::transport::SerialEndpoint;
using visio_schema::transport::test::InboundCollector;
using visio_schema::wire::Message;

TEST(Io, SerialRoundTripsAFrame) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);

  Message m;
  m.stream_id = 16;
  m.payload = "hello";
  tx.Send(m);

  ASSERT_GE(rxc.wait_for(1), 1u);
  auto got = rxc.messages();
  EXPECT_EQ(got[0].stream_id, 16u);
  EXPECT_EQ(got[0].payload, "hello");

  tx.Stop();
  rx.Stop();
}

// A fixed fd (no reconnect) reports peer EOF through the on_closed callback — it
// does not throw. The endpoint's I/O thread sees the hangup, fires the callback
// once, and exits.
TEST(Io, EofReportsClosed) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint rx(b);
  InboundCollector collector;
  rx.Start(collector.fn(), collector.on_closed());
  CloseFd(a);  // peer hangs up
  EXPECT_TRUE(collector.wait_closed());
  rx.Stop();
}

// A Send onto a broken link must never block or throw: the I/O thread drains the
// outbox best-effort, finds the fd dead, and sheds the frame. The caller's Send()
// is a thread-safe non-blocking enqueue regardless of link state.
TEST(Io, BrokenWriteSheddsWithoutThrowing) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a);
  CloseFd(b);  // peer gone -> writes to `a` will EPIPE
  tx.Start(nullptr, nullptr);

  Message m;
  m.stream_id = 16;
  m.payload = "x";
  EXPECT_NO_THROW(tx.Send(m));

  std::this_thread::sleep_for(std::chrono::milliseconds(20));
  tx.Stop();
}
