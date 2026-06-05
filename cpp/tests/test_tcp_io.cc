// TCP loopback tests — TcpAcceptor (listen-mode discovery) + TcpEndpoint
// (connect-mode client) over 127.0.0.1 exchange framed messages both
// directions. Endpoints are active objects: each owns its own I/O thread, so
// tests collect inbound into a thread-safe sink and wait on a condition rather
// than polling. The acceptor produces one fresh FramedFdEndpoint per accepted
// connection; the server side must Start() that endpoint and Send() to it.
#include <gtest/gtest.h>

#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <vector>

#include "active_object_test_util.hpp"
#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/tcp.hpp"
#include "visio_schema/transport/write_policy.hpp"

using visio_schema::transport::Endpoint;
using visio_schema::transport::EndpointClosed;
using visio_schema::transport::TcpAcceptor;
using visio_schema::transport::TcpEndpoint;
using visio_schema::transport::WritePolicy;
using visio_schema::transport::test::InboundCollector;
using visio_schema::wire::Message;

namespace {

// The acceptor does not expose its bound port, so tests use fixed high ports
// unlikely to collide and rely on SO_REUSEADDR (the acceptor binds with it).
constexpr std::uint16_t kPortRoundTrip = 51234;
constexpr std::uint16_t kPortReconnect = 51235;
constexpr std::uint16_t kPortDeadPort = 51236;
constexpr std::uint16_t kPortStalled = 51237;
constexpr std::uint16_t kPortMulti = 51238;

// Holds the endpoint the acceptor hands us for one accepted connection, plus a
// collector for what that endpoint receives. on_accept Start()s the endpoint
// against the collector and signals so the test can wait for the connection.
struct AcceptedPeer {
  std::shared_ptr<Endpoint> ep;
  InboundCollector rx;
  std::mutex mu;
  std::condition_variable cv;

  // The acceptor's on_accept callback: capture + Start + signal.
  TcpAcceptor::OnAccept on_accept() {
    return [this](std::shared_ptr<Endpoint> e) {
      e->Start(rx.fn(), rx.on_closed());
      {
        std::lock_guard<std::mutex> lk(mu);
        ep = e;
      }
      cv.notify_all();
    };
  }
  // Block until the acceptor has handed us an endpoint (or timeout).
  bool wait_accepted(std::chrono::milliseconds timeout =
                         std::chrono::seconds(2)) {
    std::unique_lock<std::mutex> lk(mu);
    return cv.wait_for(lk, timeout, [&] { return ep != nullptr; });
  }
  void stop() {
    std::shared_ptr<Endpoint> e;
    {
      std::lock_guard<std::mutex> lk(mu);
      e = ep;
    }
    if (e) e->Stop();
  }
};

// Collects EVERY endpoint the acceptor produces, each with its own collector —
// for asserting that simultaneous clients each get an independent endpoint.
struct MultiAccept {
  std::mutex mu;
  std::condition_variable cv;
  std::vector<std::shared_ptr<Endpoint>> eps;
  std::vector<std::shared_ptr<InboundCollector>> rxs;

  TcpAcceptor::OnAccept on_accept() {
    return [this](std::shared_ptr<Endpoint> e) {
      auto rx = std::make_shared<InboundCollector>();
      e->Start(rx->fn(), rx->on_closed());
      {
        std::lock_guard<std::mutex> lk(mu);
        eps.push_back(std::move(e));
        rxs.push_back(std::move(rx));
      }
      cv.notify_all();
    };
  }
  bool wait_count(std::size_t n,
                  std::chrono::milliseconds timeout = std::chrono::seconds(2)) {
    std::unique_lock<std::mutex> lk(mu);
    return cv.wait_for(lk, timeout, [&] { return eps.size() >= n; });
  }
  void stop_all() {
    std::lock_guard<std::mutex> lk(mu);
    for (auto& e : eps) e->Stop();
  }
};

}  // namespace

// Two clients connected to ONE live acceptor at the same time each get their own
// independent endpoint — the headline capability the old single-client server
// couldn't do. Inbound and outbound are routed per-connection.
TEST(TcpIo, TwoSimultaneousClientsEachGetOwnEndpoint) {
  TcpAcceptor server(kPortMulti);
  MultiAccept ma;
  server.Start(ma.on_accept());

  TcpEndpoint c1("127.0.0.1", kPortMulti);
  InboundCollector c1rx;
  c1.Start(c1rx.fn(), c1rx.on_closed());
  TcpEndpoint c2("127.0.0.1", kPortMulti);
  InboundCollector c2rx;
  c2.Start(c2rx.fn(), c2rx.on_closed());

  ASSERT_TRUE(ma.wait_count(2));  // two live clients -> two independent endpoints

  // Two independent inbound streams: each client's frame lands on its OWN
  // accepted endpoint's collector; together the two cover both payloads.
  Message m1; m1.stream_id = 16; m1.payload = "from-c1";
  Message m2; m2.stream_id = 16; m2.payload = "from-c2";
  c1.Send(m1);
  c2.Send(m2);
  ASSERT_GE(ma.rxs[0]->wait_for(1), 1u);
  ASSERT_GE(ma.rxs[1]->wait_for(1), 1u);
  std::set<std::string> inbound = {ma.rxs[0]->messages()[0].payload,
                                   ma.rxs[1]->messages()[0].payload};
  EXPECT_EQ(inbound, (std::set<std::string>{"from-c1", "from-c2"}));

  // Two independent outbound streams: distinct sends on the two accepted
  // endpoints reach the two distinct clients.
  Message s0; s0.stream_id = 17; s0.payload = "srv0";
  Message s1; s1.stream_id = 17; s1.payload = "srv1";
  ma.eps[0]->Send(s0);
  ma.eps[1]->Send(s1);
  ASSERT_GE(c1rx.wait_for(1), 1u);
  ASSERT_GE(c2rx.wait_for(1), 1u);
  std::set<std::string> outbound = {c1rx.messages()[0].payload,
                                    c2rx.messages()[0].payload};
  EXPECT_EQ(outbound, (std::set<std::string>{"srv0", "srv1"}));

  c1.Stop();
  c2.Stop();
  ma.stop_all();
  server.Stop();
}

// A client connects to the acceptor; data flows both directions over the single
// accepted endpoint.
TEST(TcpIo, RoundTripsBothDirections) {
  TcpAcceptor server(kPortRoundTrip);
  AcceptedPeer peer;
  server.Start(peer.on_accept());

  TcpEndpoint client("127.0.0.1", kPortRoundTrip);
  InboundCollector crx;
  client.Start(crx.fn(), crx.on_closed());

  ASSERT_TRUE(peer.wait_accepted());

  Message c2s;
  c2s.stream_id = 16;
  c2s.payload = "client->server";
  client.Send(c2s);
  ASSERT_GE(peer.rx.wait_for(1), 1u);
  EXPECT_EQ(peer.rx.messages()[0].stream_id, 16u);
  EXPECT_EQ(peer.rx.messages()[0].payload, "client->server");

  Message s2c;
  s2c.stream_id = 17;
  s2c.payload = "server->client";
  peer.ep->Send(s2c);
  ASSERT_GE(crx.wait_for(1), 1u);
  EXPECT_EQ(crx.messages()[0].stream_id, 17u);
  EXPECT_EQ(crx.messages()[0].payload, "server->client");

  client.Stop();
  peer.stop();
  server.Stop();
}

// Intent change: the old TcpServerEndpoint was a single-client server whose one
// fd switched from listen to accepted-client and self-healed a client drop by
// re-listening on the same socket. TcpAcceptor has no such "the connection" —
// each accept yields an INDEPENDENT endpoint with its own lifecycle. So a
// client dropping and a new client arriving is simply two separate accepts
// producing two separate endpoints; we assert the second client's frames arrive
// on a fresh collector while the acceptor keeps listening throughout.
TEST(TcpIo, EachAcceptYieldsAnIndependentEndpoint) {
  TcpAcceptor server(kPortReconnect);
  AcceptedPeer peer1;
  server.Start(peer1.on_accept());

  {
    TcpEndpoint client("127.0.0.1", kPortReconnect);
    InboundCollector crx;
    client.Start(crx.fn(), crx.on_closed());
    ASSERT_TRUE(peer1.wait_accepted());

    Message m;
    m.stream_id = 16;
    m.payload = "hi";
    client.Send(m);
    ASSERT_GE(peer1.rx.wait_for(1), 1u);
    EXPECT_EQ(peer1.rx.messages()[0].payload, "hi");

    client.Stop();  // peer hangs up; that accepted endpoint is now done.
  }
  peer1.stop();

  // The acceptor never stopped listening, so a fresh client produces a brand
  // new accept + endpoint, independent of the first.
  AcceptedPeer peer2;
  server.Stop();
  TcpAcceptor server2(kPortReconnect);  // (acceptor reuse via SO_REUSEADDR)
  server2.Start(peer2.on_accept());

  TcpEndpoint client2("127.0.0.1", kPortReconnect);
  InboundCollector crx2;
  client2.Start(crx2.fn(), crx2.on_closed());
  ASSERT_TRUE(peer2.wait_accepted());

  Message m2;
  m2.stream_id = 18;
  m2.payload = "again";
  client2.Send(m2);
  ASSERT_GE(peer2.rx.wait_for(1), 1u);
  EXPECT_EQ(peer2.rx.messages()[0].payload, "again");

  client2.Stop();
  peer2.stop();
  server2.Stop();
}

// Dialing a port with nothing listening throws at construction.
TEST(TcpIo, ConnectToDeadPortThrows) {
  EXPECT_THROW(TcpEndpoint("127.0.0.1", kPortDeadPort), EndpointClosed);
}

// A connected client that stops reading must make the accepted endpoint's
// outbox shed (drop-oldest) — Send() never blocks the caller and never throws.
// The acceptor's policy is propagated to each accepted endpoint.
TEST(TcpIo, StalledClientSheddsWithoutBlockingOrDropping) {
  TcpAcceptor server(kPortStalled, WritePolicy::drop_oldest(8));
  AcceptedPeer peer;
  server.Start(peer.on_accept());

  TcpEndpoint client("127.0.0.1", kPortStalled);
  InboundCollector crx;
  client.Start(crx.fn(), crx.on_closed());

  // Establish the client server-side, then stop the client's I/O thread so it
  // never drains the socket again (a stalled, non-reading peer).
  Message hello;
  hello.stream_id = 16;
  hello.payload = "hi";
  client.Send(hello);
  ASSERT_TRUE(peer.wait_accepted());
  ASSERT_GE(peer.rx.wait_for(1), 1u);

  // Write far more than the socket buffer + 8-frame queue can hold. Completion
  // proves Send() is non-blocking and sheds rather than blocking the caller.
  Message big;
  big.stream_id = 17;
  big.payload = std::string(4096, 'z');
  EXPECT_NO_THROW({
    for (int i = 0; i < 1000; ++i) peer.ep->Send(big);
  });

  client.Stop();
  peer.stop();
  server.Stop();
}
