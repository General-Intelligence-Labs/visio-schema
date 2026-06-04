#include "visio_schema/transport/tcp.hpp"

#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <stdexcept>

#include "visio_schema/transport/framing.hpp"

namespace visio_schema::transport {

TcpServerEndpoint::TcpServerEndpoint(std::uint16_t port, int write_timeout_ms)
    : write_timeout_ms_(write_timeout_ms) {
  listen_fd_ = OpenTcpListenSocket(port);
  if (listen_fd_ < 0) {
    throw std::runtime_error("TcpServerEndpoint: bind/listen failed");
  }
}

TcpServerEndpoint::~TcpServerEndpoint() { Close(); }

int TcpServerEndpoint::Fileno() const {
  return client_ ? client_->Fileno() : listen_fd_;
}

void TcpServerEndpoint::DropClient() {
  if (client_) client_->Close();
  client_.reset();
  rx_buf_.clear();
}

std::vector<Message> TcpServerEndpoint::TryRead() {
  if (!client_) {
    // The listen fd is readable → a connection is pending. Accept one client.
    int cfd = ::accept4(listen_fd_, nullptr, nullptr, SOCK_CLOEXEC);
    if (cfd < 0) return {};
    int one = 1;
    ::setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    client_ = MakeFdLink(cfd, /*set_raw=*/false, write_timeout_ms_);
    return {};  // no data yet — frames arrive on a later poll
  }
  std::uint8_t chunk[4096];
  const std::size_t n = client_->ReadNonblocking(chunk, sizeof(chunk));
  if (n == 0) {  // poll said readable + read returned 0 → client closed (EOF)
    DropClient();
    throw EndpointClosed("TcpServerEndpoint: client disconnected");
  }
  rx_buf_.insert(rx_buf_.end(), chunk, chunk + n);
  return ExtractFrames(rx_buf_);
}

void TcpServerEndpoint::Write(const Message& msg) {
  if (!client_) return;  // no consumer connected — drop (the MCAP leg records)
  if (!WriteFramed(*client_, msg)) {
    DropClient();
    throw EndpointClosed("TcpServerEndpoint: client write failed");
  }
}

void TcpServerEndpoint::Close() {
  DropClient();
  if (listen_fd_ >= 0) {
    ::close(listen_fd_);
    listen_fd_ = -1;
  }
}

}  // namespace visio_schema::transport
