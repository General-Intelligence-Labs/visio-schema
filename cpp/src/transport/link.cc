#include "visio_schema/transport/link.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#if defined(__linux__)
#include <sys/prctl.h>
#endif
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <termios.h>
#include <unistd.h>

#include <cerrno>

// MSG_NOSIGNAL (Linux) suppresses SIGPIPE per-send; macOS/BSD lack it and use the
// SO_NOSIGPIPE socket option instead (set by SetNoSigpipe at socket creation).
// Define it to 0 where absent so the send() call still compiles.
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

namespace visio_schema::transport {

namespace {

// FD_CLOEXEC on `fd` — portable replacement for the SOCK_CLOEXEC socket() flag
// (Linux/BSD only). Best-effort; a missing CLOEXEC is not fatal.
void SetCloexec(int fd) {
  if (fd < 0) return;
  const int fl = ::fcntl(fd, F_GETFD, 0);
  if (fl >= 0) ::fcntl(fd, F_SETFD, fl | FD_CLOEXEC);
}

// Suppress SIGPIPE on writes to a socket whose peer hung up. Linux does this
// per-send via MSG_NOSIGNAL; macOS/BSD need the SO_NOSIGPIPE socket option. No-op
// where neither exists (the MSG_NOSIGNAL send path covers it).
void SetNoSigpipe(int fd) {
#ifdef SO_NOSIGPIPE
  int one = 1;
  ::setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#else
  (void)fd;
#endif
}

}  // namespace

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

int DialTcpFd(const char* host, std::uint16_t port, int timeout_ms) {
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  if (::inet_pton(AF_INET, host, &addr.sin_addr) != 1) return -1;

  const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return -1;
  SetCloexec(fd);
  SetNoSigpipe(fd);

  if (timeout_ms > 0) {
    // Bounded connect: go non-blocking first, then poll(POLLOUT) up to timeout_ms —
    // a dropped SYN (peer not yet accepting) fails fast instead of blocking the
    // caller for the kernel SYN-retry window (~127 s).
    if (!SetNonblocking(fd)) { ::close(fd); return -1; }
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
      if (errno != EINPROGRESS) { ::close(fd); return -1; }
      pollfd pfd{fd, POLLOUT, 0};
      if (::poll(&pfd, 1, timeout_ms) <= 0) { ::close(fd); return -1; }
      int err = 0;
      socklen_t len = sizeof(err);
      if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len) != 0 || err != 0) {
        ::close(fd);
        return -1;
      }
    }
  } else {
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
      ::close(fd);
      return -1;
    }
    if (!SetNonblocking(fd)) { ::close(fd); return -1; }
  }
  int one = 1;
  ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  ::setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
  return fd;
}

std::pair<int, int> MakeFdPair() {
  int sv[2] = {-1, -1};
  if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) return {-1, -1};
  for (int fd : sv) {
    SetNonblocking(fd);
    SetCloexec(fd);
    SetNoSigpipe(fd);
  }
  return {sv[0], sv[1]};
}

int OpenTcpListenSocket(std::uint16_t port) {
  const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return -1;
  SetCloexec(fd);
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

int AcceptCloexec(int listen_fd) {
#if defined(__linux__)
  return ::accept4(listen_fd, nullptr, nullptr, SOCK_CLOEXEC);
#else
  const int fd = ::accept(listen_fd, nullptr, nullptr);
  if (fd >= 0) {
    SetCloexec(fd);
    SetNoSigpipe(fd);
  }
  return fd;
#endif
}

void SetCurrentThreadName(const char* name) {
#if defined(__linux__)
  ::prctl(PR_SET_NAME, name, 0, 0, 0);
#else
  (void)name;
#endif
}

}  // namespace visio_schema::transport
