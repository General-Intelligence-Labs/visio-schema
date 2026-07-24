#include "visio_schema/transport/tcp.hpp"

#include "visio_schema/transport/link.hpp"  // SetCurrentThreadName

#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
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
  wake_.Open();
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
  wake_.Close();
}

void TcpAcceptor::Wake() { wake_.Signal(); }

void TcpAcceptor::Loop() {
  SetCurrentThreadName("vs_tcp_accept");
  while (!stop_.load()) {
    pollfd pfds[2] = {{wake_.poll_fd(), POLLIN, 0}, {listen_fd_, POLLIN, 0}};
    ::poll(pfds, 2, kTickMs);
    if (pfds[0].revents & POLLIN) wake_.Drain();
    if (stop_.load()) break;
    if (!(pfds[1].revents & POLLIN)) continue;

    // Accept every pending connection; each becomes its own endpoint.
    for (;;) {
      int cfd = AcceptCloexec(listen_fd_);
      if (cfd < 0) break;  // EAGAIN: no more pending
      int one = 1;
      ::setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
      // Bound the kernel send buffer so a slow/lossy client can't bank seconds
      // of live H.265 below the app-level outbox eviction (observed ~600 KB
      // autotuned → ~0.5-1 s of standing latency on the Wi-Fi AP). 256 KB is
      // well above the link BDP, so it doesn't throttle throughput; it just caps
      // how much the kernel can hoard. Disables SO_SNDBUF autotuning.
      int sndbuf = 256 * 1024;
      ::setsockopt(cfd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));
      // A phone that vanishes without FIN (airplane mode, walked off the AP)
      // must free its endpoint promptly — the product allows ONE client at a
      // time, so a zombie link blocks the next connect. This device is
      // nearly always sending, so TCP_USER_TIMEOUT (bounds how long unacked
      // data may sit) is the detector that actually fires: the socket errors,
      // the endpoint's I/O thread exits, the bus forgets it. Keepalive covers
      // the fully-idle case. ~10 s total: also the worst-case lockout when a
      // phone hops transports (Wi-Fi -> NCM) and its old link must die first.
      int one_ka = 1;
      ::setsockopt(cfd, SOL_SOCKET, SO_KEEPALIVE, &one_ka, sizeof(one_ka));
#if defined(__linux__)
      int user_timeout_ms = 10 * 1000;
      ::setsockopt(cfd, IPPROTO_TCP, TCP_USER_TIMEOUT, &user_timeout_ms,
                   sizeof(user_timeout_ms));
      int idle_s = 5, intvl_s = 2, cnt = 3;  // idle links: dead in ~11 s
      ::setsockopt(cfd, IPPROTO_TCP, TCP_KEEPIDLE, &idle_s, sizeof(idle_s));
      ::setsockopt(cfd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl_s, sizeof(intvl_s));
      ::setsockopt(cfd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
#endif
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
