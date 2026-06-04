#include "visio_schema/transport/link.hpp"

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pty.h>
#include <sys/socket.h>
#include <termios.h>
#include <unistd.h>

#include <cerrno>
#include <iostream>
#include <stdexcept>

namespace visio_schema::transport {

namespace {

class FdLink : public Link {
 public:
  FdLink(int fd, bool set_raw, int write_timeout_ms, bool nonblocking = false)
      : fd_(fd), write_timeout_ms_(write_timeout_ms) {
    if (set_raw) {
      termios tio{};
      if (tcgetattr(fd_, &tio) == 0) {
        cfmakeraw(&tio);
        tcsetattr(fd_, TCSANOW, &tio);
      }
    }
    if (nonblocking && !SetNonblocking(fd_)) {
      std::cerr << "FdLink: failed to set O_NONBLOCK on fd " << fd_ << "\n";
    }
  }

  ~FdLink() override { Close(); }

  int Fileno() const override { return fd_; }

  std::size_t ReadNonblocking(std::uint8_t* buf, std::size_t max_bytes) override {
    while (true) {
      ssize_t n = ::read(fd_, buf, max_bytes);
      if (n > 0) return static_cast<std::size_t>(n);
      if (n == 0) return 0;
      if (errno == EINTR) continue;
      return 0;
    }
  }

  long ReadSome(std::uint8_t* buf, std::size_t max_bytes) override {
    while (true) {
      ssize_t n = ::read(fd_, buf, max_bytes);
      if (n > 0) return static_cast<long>(n);
      if (n == 0) return -1;  // EOF
      if (errno == EINTR) continue;
      if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;  // no data yet
      return -1;              // dead link
    }
  }

  long WriteSome(const std::uint8_t* data, std::size_t len) override {
    while (true) {
      ssize_t n = ::write(fd_, data, len);
      if (n >= 0) return static_cast<long>(n);
      if (errno == EINTR) continue;
      if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;  // send buf full
      return -1;              // EPIPE / EIO / ENODEV / ECONNRESET ...
    }
  }

  bool Write(std::string_view data) override {
    std::size_t off = 0;
    while (off < data.size()) {
      if (write_timeout_ms_ > 0) {
        pollfd pfd{fd_, POLLOUT, 0};
        int r = ::poll(&pfd, 1, write_timeout_ms_);
        if (r < 0) {
          if (errno == EINTR) continue;
          return false;
        }
        if (r == 0) return false;  // send buffer full past timeout — stalled
        if (pfd.revents & (POLLERR | POLLHUP | POLLNVAL)) return false;
      }
      ssize_t n = ::write(fd_, data.data() + off, data.size() - off);
      if (n < 0) {
        if (errno == EINTR) continue;
        return false;
      }
      if (n == 0) return false;
      off += static_cast<std::size_t>(n);
    }
    return true;
  }

  void Close() override {
    if (fd_ >= 0) {
      ::close(fd_);
      fd_ = -1;
    }
  }

 private:
  int fd_;
  int write_timeout_ms_;
};

}  // namespace

bool SetNonblocking(int fd) {
  const int fl = ::fcntl(fd, F_GETFL, 0);
  if (fl < 0) return false;
  return ::fcntl(fd, F_SETFL, fl | O_NONBLOCK) == 0;
}

std::pair<std::shared_ptr<Link>, std::shared_ptr<Link>> MakeFdLinkPair() {
  int master = -1, slave = -1;
  if (openpty(&master, &slave, nullptr, nullptr, nullptr) != 0) {
    throw std::runtime_error("openpty() failed");
  }
  return {std::make_shared<FdLink>(master, /*set_raw=*/true, /*write_timeout_ms=*/0),
          std::make_shared<FdLink>(slave, /*set_raw=*/true, /*write_timeout_ms=*/0)};
}

std::shared_ptr<Link> MakeFdLink(int fd, bool set_raw, int write_timeout_ms,
                                 bool nonblocking) {
  return std::make_shared<FdLink>(fd, set_raw, write_timeout_ms, nonblocking);
}

std::shared_ptr<Link> OpenFdLink(const char* path, bool set_raw,
                                 int write_timeout_ms, bool nonblocking) {
  int fd = ::open(path, O_RDWR | O_NOCTTY | O_CLOEXEC);
  if (fd < 0) return nullptr;
  return std::make_shared<FdLink>(fd, set_raw, write_timeout_ms, nonblocking);
}

std::shared_ptr<Link> OpenTcpClientLink(const char* host, std::uint16_t port,
                                        int write_timeout_ms, bool nonblocking) {
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  if (::inet_pton(AF_INET, host, &addr.sin_addr) != 1) return nullptr;

  int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (fd < 0) return nullptr;
  if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    ::close(fd);
    return nullptr;
  }
  int one = 1;
  ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  ::setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));
  return std::make_shared<FdLink>(fd, /*set_raw=*/false, write_timeout_ms,
                                  nonblocking);
}

int OpenTcpListenSocket(std::uint16_t port) {
  int fd = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
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
