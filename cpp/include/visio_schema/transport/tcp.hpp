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
#include "visio_schema/transport/link.hpp"

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
    auto link = OpenTcpClientLink(host.c_str(), port, write_timeout_ms);
    if (!link) throw EndpointClosed("TcpEndpoint: connect to " + host + " failed");
    return link;
  }
};

// Listen-mode TCP server, single client at a time. With no client, Fileno()
// returns the listen fd so poll() wakes on an incoming connection; once a client
// is accepted Fileno() returns its fd. A client disconnect throws EndpointClosed.
class TcpServerEndpoint : public Endpoint {
 public:
  // Binds + listens immediately; throws std::runtime_error on bind/listen fail.
  explicit TcpServerEndpoint(std::uint16_t port, int write_timeout_ms = 200);
  ~TcpServerEndpoint() override;

  int Fileno() const override;
  std::vector<Message> TryRead() override;   // throws EndpointClosed when a client drops
  void Write(const Message& msg) override;   // throws EndpointClosed on client write fail
  void Close() override;

  bool has_client() const { return static_cast<bool>(client_); }

 private:
  void DropClient();

  int listen_fd_ = -1;
  int write_timeout_ms_;
  std::shared_ptr<Link> client_;
  std::vector<std::uint8_t> rx_buf_;
};

}  // namespace visio_schema::transport
