// TCP transports for the Visio stack.
//
//   TcpEndpoint  — connect-mode client (an Endpoint): dials host:port once and
//                  reframes over it (e.g. a hub dialing a leaf). A
//                  FramedFdEndpoint over a freshly dialed link.
//
//   TcpAcceptor  — listen-mode DISCOVERY, NOT an Endpoint. Owns a listen socket
//                  + its own accept thread. On each accepted connection it builds
//                  a fresh FramedFdEndpoint over the client fd and hands it to
//                  on_accept(); the owner (hub) attaches it to the bus as a peer.
//                  When that client disconnects the endpoint fires on_closed and
//                  the bus forgets it; the acceptor keeps listening. Multiple
//                  clients => multiple endpoints, each with its own identity and
//                  lifecycle — the accept loop never owns "the connection".
//
// Accepted connections speak the same COBS-delimited core-frame format as
// SerialEndpoint and TcpEndpoint (they ARE FramedFdEndpoints).
#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <thread>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/framed_fd.hpp"
#include "visio_schema/transport/link.hpp"  // DialTcpFd, OpenTcpListenSocket, FdFactory
#include "visio_schema/transport/wake_fd.hpp"  // pollable cross-thread wakeup
#include "visio_schema/transport/write_policy.hpp"

namespace visio_schema::transport {

// Connect-mode TCP client. Dials at construction; throws EndpointClosed if the
// peer isn't up. A fixed-fd endpoint (a drop surfaces via on_closed; the owner
// re-dials).
class TcpEndpoint : public FramedFdEndpoint {
 public:
  TcpEndpoint(const std::string& host, std::uint16_t port)
      : FramedFdEndpoint(Dial(host, port)) {}

 private:
  static int Dial(const std::string& host, std::uint16_t port) {
    const int fd = DialTcpFd(host.c_str(), port);
    if (fd < 0) throw EndpointClosed("TcpEndpoint: connect to " + host + " failed");
    return fd;
  }
};

// Which local address a connection landed on. A device serving several links at
// once (setup AP, station Wi-Fi, USB-NCM) cannot tell them apart from the
// Endpoint alone, and an occupancy rule that treats every client as
// interchangeable then locks a phone out of its own session when it moves from
// one link to another. Captured at accept() from getsockname(), before anything
// can close the fd.
struct AcceptedLeg {
  // Dotted-quad local address the client reached us on ("" if unavailable) —
  // e.g. the AP gateway vs the station lease. This is the LEG identity.
  std::string local_ip;
  // Dotted-quad remote address ("" if unavailable), for logging.
  std::string peer_ip;
};

// Listen-mode acceptor. Produces one FramedFdEndpoint per accepted connection.
class TcpAcceptor {
 public:
  // on_accept(endpoint, who) is called from the accept thread for each new
  // client; it must attach the endpoint somewhere (e.g. bus.AttachPeer) — the
  // acceptor keeps no reference to it.
  using OnAccept =
      std::function<void(std::shared_ptr<Endpoint>, const AcceptedLeg&)>;

  // Pre-admission check, called on the accept thread with each connection's
  // leg identity BEFORE the endpoint (socket options, outboxes) is built.
  // Return false to refuse: the fd is closed on the spot and the listen
  // socket is left unpolled for one tick, so a client redialing in a tight
  // loop (a reconnect storm) costs a handful of syscalls per tick instead of
  // an endpoint construction per attempt. No gate = admit everything;
  // on_accept still makes the final attach/refuse decision either way.
  using AdmissionGate = std::function<bool(const AcceptedLeg&)>;

  explicit TcpAcceptor(std::uint16_t port,
                       WritePolicy policy = WritePolicy::drop_oldest());
  ~TcpAcceptor();

  TcpAcceptor(const TcpAcceptor&) = delete;
  TcpAcceptor& operator=(const TcpAcceptor&) = delete;

  // Spawn the accept thread. The gate is fixed for the acceptor's lifetime —
  // passing it here (rather than a setter) makes that structural.
  void Start(OnAccept on_accept, AdmissionGate gate = {});
  void Stop();                     // stop + join

 private:
  void Loop();
  void Wake();

  std::uint16_t port_;
  WritePolicy policy_;
  int listen_fd_ = -1;
  WakeFd wake_;
  AdmissionGate gate_;
  OnAccept on_accept_;
  std::thread thread_;
  std::atomic<bool> stop_{false};
};

}  // namespace visio_schema::transport
