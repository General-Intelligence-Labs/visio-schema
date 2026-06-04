#include "visio_schema/transport/tcp.hpp"

#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <stdexcept>
#include <string>

#include "visio_schema/transport/framing.hpp"

namespace visio_schema::transport {

TcpServerEndpoint::TcpServerEndpoint(std::uint16_t port, WritePolicy policy)
    : outbox_(policy) {
  listen_fd_ = OpenTcpListenSocket(port);
  if (listen_fd_ < 0) {
    throw std::runtime_error("TcpServerEndpoint: bind/listen failed");
  }
}

TcpServerEndpoint::~TcpServerEndpoint() { Close(); }

int TcpServerEndpoint::Fileno() const {
  return client_ ? client_->Fileno() : listen_fd_;
}

short TcpServerEndpoint::PollEvents() const {
  if (!client_) return listen_fd_ >= 0 ? POLLIN : 0;  // wait for a connection
  short ev = POLLIN;
  if (outbox_.HasPending()) ev |= POLLOUT;
  return ev;
}

void TcpServerEndpoint::DropClient() {
  if (client_) client_->Close();
  client_.reset();
  rx_buf_.clear();
  outbox_.Clear();  // the next client is a fresh reader — no stale half-frame
}

std::vector<Message> TcpServerEndpoint::TryRead() {
  if (!client_) {
    // The listen fd is readable → a connection is pending. Accept one client,
    // non-blocking (reactor I/O on the bus thread must never block).
    int cfd = ::accept4(listen_fd_, nullptr, nullptr, SOCK_CLOEXEC);
    if (cfd < 0) return {};
    int one = 1;
    ::setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    client_ = MakeFdLink(cfd, /*set_raw=*/false, /*write_timeout_ms=*/0,
                         /*nonblocking=*/true);
    return {};  // no data yet — frames arrive on a later poll
  }
  std::uint8_t chunk[4096];
  const long n = client_->ReadSome(chunk, sizeof(chunk));
  if (n == 0) return {};  // EAGAIN — nothing ready
  if (n < 0) {            // EOF / dead client: drop it and resume listening
    DropClient();
    return {};
  }
  rx_buf_.insert(rx_buf_.end(), chunk, chunk + n);
  return ExtractFrames(rx_buf_);
}

void TcpServerEndpoint::Write(const Message& msg) {
  if (!client_) return;  // no consumer connected — drop (the MCAP leg records)
  const auto framed = EncodeFramed(msg);
  outbox_.Enqueue(framed.data(), framed.size());
  Pump();
}

void TcpServerEndpoint::OnWritable() { Pump(); }

void TcpServerEndpoint::Pump() {
  if (!client_) return;
  // `lk` outlives this Drain: client_ is only reset by DropClient() below, after
  // Drain() returns synchronously.
  Link* lk = client_.get();
  const bool alive = outbox_.Drain(
      [lk](const std::uint8_t* p, std::size_t n) { return lk->WriteSome(p, n); });
  if (!alive) DropClient();
}

void TcpServerEndpoint::Close() {
  DropClient();
  if (listen_fd_ >= 0) {
    ::close(listen_fd_);
    listen_fd_ = -1;
  }
}

}  // namespace visio_schema::transport
