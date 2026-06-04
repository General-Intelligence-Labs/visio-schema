// TCP transports for the Visio stack.
//
//   TcpEndpoint        — connect-mode: dials host:port once and reframes over it
//                        (e.g. a hub dialing a leaf). A FramedFdEndpoint over a
//                        freshly dialed link; no self-reconnect — a broken link
//                        throws EndpointClosed and the app re-dials.
//   TcpServerEndpoint  — listen-mode: owns a listening socket and serves one
//                        client at a time. A client disconnect throws
//                        EndpointClosed (the caller detaches + re-attaches a
//                        fresh server for the next client).
//
// Both speak the same COBS-delimited core-frame format as SerialEndpoint.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/framed_fd.hpp"
#include "visio_schema/transport/framed_outbox.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/write_policy.hpp"

namespace visio_schema::transport {

// Connect-mode TCP client. Dials at construction; throws EndpointClosed if the
// peer isn't up. Attach as a sink; it also serves inbound on the same socket.
class TcpEndpoint : public FramedFdEndpoint {
 public:
  TcpEndpoint(const std::string& host, std::uint16_t port,
              int write_timeout_ms = 200)
      : FramedFdEndpoint(Dial(host, port, write_timeout_ms)) {}

 private:
  static std::shared_ptr<Link> Dial(const std::string& host, std::uint16_t port,
                                    int write_timeout_ms) {
    auto link = OpenTcpClientLink(host.c_str(), port, write_timeout_ms,
                                  /*nonblocking=*/true);
    if (!link) throw EndpointClosed("TcpEndpoint: connect to " + host + " failed");
    return link;
  }
};

// Listen-mode TCP server, single client at a time, as a self-healing reactor
// sink. With no client, Fileno() returns the listen fd so poll() wakes on an
// incoming connection; once a client is accepted Fileno() returns its fd. A
// client that drops is dropped silently (NOT thrown) and the server returns to
// listening for the next one — so a long-lived sink is never detached by the bus.
// Writes go through a bounded outbox (write_policy): the accepted client's fd is
// non-blocking and a slow/stalled client sheds frames instead of blocking the bus.
class TcpServerEndpoint : public Endpoint {
 public:
  // Binds + listens immediately; throws std::runtime_error on bind/listen fail.
  explicit TcpServerEndpoint(std::uint16_t port,
                             WritePolicy policy = WritePolicy::drop_oldest());
  ~TcpServerEndpoint() override;

  int Fileno() const override;
  short PollEvents() const override;
  std::vector<Message> TryRead() override;   // accepts a client / reads frames; never throws on drop
  void Write(const Message& msg) override;    // enqueue; never blocks/throws
  void OnWritable() override;
  void Close() override;

  bool has_client() const { return static_cast<bool>(client_); }

 private:
  void DropClient();
  void Pump();  // best-effort non-blocking drain of the outbox to the client

  int listen_fd_ = -1;
  std::shared_ptr<Link> client_;
  FramedOutbox outbox_;
  std::vector<std::uint8_t> rx_buf_;
};

}  // namespace visio_schema::transport
