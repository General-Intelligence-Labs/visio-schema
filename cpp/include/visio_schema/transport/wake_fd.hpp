// WakeFd — a pollable wakeup primitive that interrupts a poll() loop from another
// thread. Linux uses a single eventfd; elsewhere (macOS/BSD) a non-blocking
// self-pipe. This is the one place the eventfd/self-pipe split lives, so
// FramedFdEndpoint and TcpAcceptor stay portable without per-file #ifdefs.
//
// Usage: Open() once before the loop; add poll_fd() to the pollfd set (POLLIN);
// Signal() from any thread to wake the loop; Drain() on the loop thread to clear
// pending wakeups; Close() at teardown.
#pragma once

#include <cstdint>

#include <fcntl.h>
#include <unistd.h>
#if defined(__linux__)
#include <sys/eventfd.h>
#endif

namespace visio_schema::transport {

class WakeFd {
 public:
  WakeFd() = default;
  ~WakeFd() { Close(); }
  WakeFd(const WakeFd&) = delete;
  WakeFd& operator=(const WakeFd&) = delete;

  // Open the primitive (no-op if already open). Returns false on failure.
  bool Open() {
    if (read_fd_ >= 0) return true;
#if defined(__linux__)
    read_fd_ = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    write_fd_ = read_fd_;  // one fd serves both read and write
    return read_fd_ >= 0;
#else
    int fds[2] = {-1, -1};
    if (::pipe(fds) != 0) return false;
    for (int fd : fds) {
      ::fcntl(fd, F_SETFL, ::fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
      ::fcntl(fd, F_SETFD, FD_CLOEXEC);
    }
    read_fd_ = fds[0];
    write_fd_ = fds[1];
    return true;
#endif
  }

  int poll_fd() const { return read_fd_; }
  bool is_open() const { return read_fd_ >= 0; }

  // Poke the loop (any thread, non-blocking). A full pipe just means a wakeup is
  // already pending. Writing 8 bytes works for both eventfd (requires 8) and a
  // pipe (any size).
  void Signal() {
    if (write_fd_ < 0) return;
    const std::uint64_t one = 1;
    (void)::write(write_fd_, &one, sizeof(one));
  }

  // Clear all pending wakeups (loop thread only). The buffer is >= 8 bytes so a
  // single eventfd counter read fits; a pipe drains until EAGAIN.
  void Drain() {
    if (read_fd_ < 0) return;
    std::uint8_t buf[256];
    while (::read(read_fd_, buf, sizeof(buf)) > 0) { /* drain */ }
  }

  void Close() {
    if (write_fd_ >= 0 && write_fd_ != read_fd_) ::close(write_fd_);
    if (read_fd_ >= 0) ::close(read_fd_);
    read_fd_ = write_fd_ = -1;
  }

 private:
  int read_fd_ = -1;
  int write_fd_ = -1;
};

}  // namespace visio_schema::transport
