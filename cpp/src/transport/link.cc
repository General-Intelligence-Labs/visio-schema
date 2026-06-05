#include "visio_schema/transport/link.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <termios.h>
#include <unistd.h>

#include <cerrno>

namespace visio_schema::transport {

bool SetNonblocking(int fd) {
  const int fl = ::fcntl(fd, F_GETFL, 0);
  if (fl < 0) return false;
  return ::fcntl(fd, F_SETFL, fl | O_NONBLOCK) == 0;
}

long WriteSome(int fd, const std::uint8_t* data, std::size_t len) {
  for (;;) {
    // send(MSG_NOSIGNAL) so a write to a socket whose peer hung up returns EPIPE
    // instead of raising SIGPIPE (which would kill the process). Falls back to
    // write() for non-sockets (serial / pty / regular fd → ENOTSOCK).
    ssize_t n = ::send(fd, data, len, MSG_NOSIGNAL);
    if (n < 0 && errno == ENOTSOCK) n = ::write(fd, data, len);
    if (n >= 0) return static_cast<long>(n);
    if (errno == EINTR) continue;
    if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;  // send buffer full
    return -1;  // EPIPE / EIO / ENODEV / ECONNRESET ...
  }
}

long ReadSome(int fd, std::uint8_t* buf, std::size_t max_bytes) {
  for (;;) {
    const ssize_t n = ::read(fd, buf, max_bytes);
    if (n > 0) return static_cast<long>(n);
    if (n == 0) return -1;  // EOF
    if (errno == EINTR) continue;
    if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;  // no data yet
    return -1;  // dead fd
  }
}

void CloseFd(int fd) {
  if (fd < 0) return;
  // Drop any queued output before closing: a CDC-ACM gadget close (gs_close)
  // otherwise BLOCKS draining the TX buffer when no host is reading. No-op
  // (ENOTTY) on sockets / regular fds.
  ::tcflush(fd, TCOFLUSH);
  ::close(fd);
}

void SetRawMode(int fd) {
  termios tio{};
  if (::tcgetattr(fd, &tio) == 0) {  // ENOTTY on non-ttys → no-op
    ::cfmakeraw(&tio);
    ::tcsetattr(fd, TCSANOW, &tio);
  }
}

int OpenSerialFd(const char* path) {
  const int fd = ::open(path, O_RDWR | O_NOCTTY | O_CLOEXEC);
  if (fd < 0) return -1;
  SetRawMode(fd);
  if (!SetNonblocking(fd)) {
    ::close(fd);
    return -1;
  }
  return fd;
}

int DialTcpFd(const char* host, std::uint16_t port) {
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  if (::inet_pton(AF_INET, host, &addr.sin_addr) != 1) return -1;

  const int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (fd < 0) return -1;
  if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    ::close(fd);
    return -1;
  }
  int one = 1;
  ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  ::setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
  if (!SetNonblocking(fd)) {
    ::close(fd);
    return -1;
  }
  return fd;
}

std::pair<int, int> MakeFdPair() {
  int sv[2] = {-1, -1};
  if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) return {-1, -1};
  SetNonblocking(sv[0]);
  SetNonblocking(sv[1]);
  return {sv[0], sv[1]};
}

int OpenTcpListenSocket(std::uint16_t port) {
  const int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (fd < 0) return -1;
  int one = 1;
  ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(port);
  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
      ::listen(fd, /*backlog=*/4) != 0) {
    ::close(fd);
    return -1;
  }
  return fd;
}

}  // namespace visio_schema::transport
