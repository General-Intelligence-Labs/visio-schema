#include "visio_schema/transport/tcp.hpp"

#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <unistd.h>

#include <iostream>
#include <stdexcept>
#include <utility>

namespace visio_schema::transport {

namespace {
constexpr int kTickMs = 200;
}

TcpAcceptor::TcpAcceptor(std::uint16_t port, WritePolicy policy)
    : port_(port), policy_(policy) {
  listen_fd_ = OpenTcpListenSocket(port);
  if (listen_fd_ < 0) {
    throw std::runtime_error("TcpAcceptor: bind/listen failed");
  }
  SetNonblocking(listen_fd_);  // so the drain-accept loop never blocks
}

TcpAcceptor::~TcpAcceptor() { Stop(); }

void TcpAcceptor::Start(OnAccept on_accept) {
  on_accept_ = std::move(on_accept);
  if (wake_fd_ < 0) wake_fd_ = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
  stop_.store(false);
  thread_ = std::thread([this] { Loop(); });
}

void TcpAcceptor::Stop() {
  stop_.store(true);
  Wake();
  if (thread_.joinable()) thread_.join();
  if (listen_fd_ >= 0) {
    ::close(listen_fd_);
    listen_fd_ = -1;
  }
  if (wake_fd_ >= 0) {
    ::close(wake_fd_);
    wake_fd_ = -1;
  }
}

void TcpAcceptor::Wake() {
  if (wake_fd_ < 0) return;
  const std::uint64_t one = 1;
  (void)::write(wake_fd_, &one, sizeof(one));
}

void TcpAcceptor::Loop() {
  while (!stop_.load()) {
    pollfd pfds[2] = {{wake_fd_, POLLIN, 0}, {listen_fd_, POLLIN, 0}};
    ::poll(pfds, 2, kTickMs);
    if (pfds[0].revents & POLLIN) {
      std::uint64_t drain;
      while (::read(wake_fd_, &drain, sizeof(drain)) > 0) { /* drain */ }
    }
    if (stop_.load()) break;
    if (!(pfds[1].revents & POLLIN)) continue;

    // Accept every pending connection; each becomes its own endpoint.
    for (;;) {
      int cfd = ::accept4(listen_fd_, nullptr, nullptr, SOCK_CLOEXEC);
      if (cfd < 0) break;  // EAGAIN: no more pending
      int one = 1;
      ::setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
      // Fixed fd (no factory): a client EOF reports on_closed once and the
      // endpoint's I/O thread exits; the bus forgets it. The acceptor keeps
      // listening for the next client. FramedFdEndpoint takes ownership of cfd
      // (and sets O_NONBLOCK).
      auto ep = std::make_shared<FramedFdEndpoint>(cfd, policy_);
      if (on_accept_) on_accept_(std::move(ep));
    }
  }
}

}  // namespace visio_schema::transport
