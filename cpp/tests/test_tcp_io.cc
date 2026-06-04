// TCP loopback tests — TcpServerEndpoint + TcpEndpoint over 127.0.0.1 exchange
// framed messages both directions; the server's Fileno() switches from the
// listen fd to the accepted client fd; a client disconnect surfaces as
// EndpointClosed; and dialing a dead port throws. Standalone (no bus): the
// underlying links are blocking and gated on poll() readability, so the test
// polls each endpoint's current fd before TryRead, exactly as the bus loop does.
#include <gtest/gtest.h>
#include <poll.h>

#include <cstdint>
#include <memory>
#include <vector>

#include <string>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/tcp.hpp"
#include "visio_schema/transport/write_policy.hpp"

using visio_schema::transport::Endpoint;
using visio_schema::transport::EndpointClosed;
using visio_schema::transport::TcpEndpoint;
using visio_schema::transport::TcpServerEndpoint;
using visio_schema::transport::WritePolicy;
using visio_schema::wire::Message;

namespace {

// Poll the endpoint's CURRENT fd, then TryRead when readable. May throw
// EndpointClosed (EOF). Returns whatever TryRead yielded (possibly empty, e.g.
// the server accepting a client).
std::vector<Message> PollRead(Endpoint& ep, int timeout_ms = 50) {
  pollfd pfd{ep.Fileno(), POLLIN, 0};
  if (::poll(&pfd, 1, timeout_ms) <= 0) return {};
  return ep.TryRead();
}

// Read one framed message, polling up to ~2 s. Returns false on timeout.
bool ReadOne(Endpoint& ep, Message* out) {
  for (int i = 0; i < 40; ++i) {
    auto got = PollRead(ep);
    if (!got.empty()) {
      *out = got[0];
      return true;
    }
  }
  return false;
}

// Bind a server on the first free port in the private range; report the port.
std::unique_ptr<TcpServerEndpoint> ServerOnFreePort(std::uint16_t* port) {
  for (std::uint16_t p = 49152; p < 49352; ++p) {
    try {
      auto s = std::make_unique<TcpServerEndpoint>(p);
      *port = p;
      return s;
    } catch (const std::exception&) { /* busy — try the next port */ }
  }
  return nullptr;
}

}  // namespace

TEST(TcpIo, RoundTripsBothDirections) {
  std::uint16_t port = 0;
  auto server = ServerOnFreePort(&port);
  ASSERT_TRUE(server) << "no free port in range";
  EXPECT_FALSE(server->has_client());

  TcpEndpoint client("127.0.0.1", port);
  Message c2s;
  c2s.stream_id = 16;
  c2s.payload = "client->server";
  client.Write(c2s);

  // The first server poll accepts the pending client (empty read); the next
  // returns the buffered frame — ReadOne spans both.
  Message got;
  ASSERT_TRUE(ReadOne(*server, &got));
  EXPECT_EQ(got.stream_id, 16u);
  EXPECT_EQ(got.payload, "client->server");
  EXPECT_TRUE(server->has_client());

  Message s2c;
  s2c.stream_id = 17;
  s2c.payload = "server->client";
  server->Write(s2c);
  Message back;
  ASSERT_TRUE(ReadOne(client, &back));
  EXPECT_EQ(back.stream_id, 17u);
  EXPECT_EQ(back.payload, "server->client");
}

TEST(TcpIo, FilenoSwitchesFromListenToClient) {
  std::uint16_t port = 0;
  auto server = ServerOnFreePort(&port);
  ASSERT_TRUE(server);
  const int listen_fileno = server->Fileno();  // listen fd while clientless

  TcpEndpoint client("127.0.0.1", port);
  Message m;
  m.stream_id = 16;
  m.payload = "x";
  client.Write(m);
  Message got;
  ASSERT_TRUE(ReadOne(*server, &got));

  EXPECT_TRUE(server->has_client());
  EXPECT_NE(server->Fileno(), listen_fileno);  // now the accepted client fd
}

TEST(TcpIo, ClientDisconnectSelfHealsAndReAccepts) {
  std::uint16_t port = 0;
  auto server = ServerOnFreePort(&port);
  ASSERT_TRUE(server);

  auto client = std::make_unique<TcpEndpoint>("127.0.0.1", port);
  Message m;
  m.stream_id = 16;
  m.payload = "hi";
  client->Write(m);
  Message got;
  ASSERT_TRUE(ReadOne(*server, &got));   // establishes the client
  ASSERT_TRUE(server->has_client());

  client->Close();   // peer hangs up

  // A self-healing reactor sink drops the client (no throw) and returns to
  // listening — it is never detached, so a long-lived server survives churn.
  for (int i = 0; i < 40 && server->has_client(); ++i) PollRead(*server);
  EXPECT_FALSE(server->has_client());

  // And it accepts a fresh client on the same listen socket.
  auto client2 = std::make_unique<TcpEndpoint>("127.0.0.1", port);
  Message m2;
  m2.stream_id = 18;
  m2.payload = "again";
  client2->Write(m2);
  Message got2;
  ASSERT_TRUE(ReadOne(*server, &got2));
  EXPECT_EQ(got2.payload, "again");
  EXPECT_TRUE(server->has_client());
}

TEST(TcpIo, ConnectToDeadPortThrows) {
  std::uint16_t port = 0;
  {
    auto s = ServerOnFreePort(&port);
    ASSERT_TRUE(s);
  }  // server destroyed: the port is now free with nothing listening
  EXPECT_THROW(TcpEndpoint("127.0.0.1", port), EndpointClosed);
}

TEST(TcpIo, StalledClientSheddsWithoutBlockingOrDropping) {
  // A connected client that stops reading must make the server's outbox shed
  // (drop-oldest) — never block the bus thread, never disconnect the client.
  std::uint16_t port = 0;
  std::unique_ptr<TcpServerEndpoint> server;
  for (std::uint16_t p = 49152; p < 49352 && !server; ++p) {
    try {
      server = std::make_unique<TcpServerEndpoint>(p, WritePolicy::drop_oldest(8));
      port = p;
    } catch (const std::exception&) { /* busy — next port */ }
  }
  ASSERT_TRUE(server);

  TcpEndpoint client("127.0.0.1", port);
  Message hello;
  hello.stream_id = 16;
  hello.payload = "hi";
  client.Write(hello);
  Message got;
  ASSERT_TRUE(ReadOne(*server, &got));  // establishes the client server-side
  ASSERT_TRUE(server->has_client());

  // Client never reads again. Write far more than the socket buffer + 8-frame
  // queue can hold. Completion proves non-blocking; has_client proves not dropped.
  Message big;
  big.stream_id = 17;
  big.payload = std::string(4096, 'z');
  EXPECT_NO_THROW({
    for (int i = 0; i < 1000; ++i) server->Write(big);
  });
  EXPECT_TRUE(server->has_client());  // stalled != broken
}
