// WakeFd — a pollable wakeup primitive that interrupts a poll() loop from another
// thread. Linux uses a single eventfd; elsewhere (macOS/BSD) a non-blocking
// self-pipe. This is the one place the eventfd/self-pipe split lives, so
// FramedFdEndpoint and TcpAcceptor stay portable without per-file #ifdefs.
//
// Usage: Open() once before the loop; add poll_fd() to the pollfd set (POLLIN);
// Signal() from any thread to wake the loop; Drain() on the loop thread to clear
// pending wakeups; Close() at teardown.
#pragma once

#include <atomic>
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
    // A Signal() racing a Close() can latch the flag after Close reset it;
    // left set across a reopen it would elide every future write against an
    // empty fd (permanent poll-tick latency). Fresh fd, fresh flag.
    signalled_.store(false, std::memory_order_release);
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

  // Poke the loop (any thread, non-blocking). Coalesced: while a wakeup is
  // already pending, further Signal()s are one atomic exchange and NO write(2)
  // — on the device the endpoint loop is woken per enqueued message
  // (hundreds/s), so every producer that lands while the loop is still busy
  // saves a syscall. Writing 8 bytes works for both eventfd (requires 8) and
  // a pipe (any size); a full pipe just means a wakeup is already pending.
  void Signal() {
    if (write_fd_ < 0) return;
    if (signalled_.exchange(true, std::memory_order_acq_rel)) return;
    const std::uint64_t one = 1;
    (void)::write(write_fd_, &one, sizeof(one));
  }

  // Clear all pending wakeups (loop thread only). The buffer is >= 8 bytes so a
  // single eventfd counter read fits; a pipe drains until EAGAIN.
  void Drain() {
    if (read_fd_ < 0) return;
    std::uint8_t buf[256];
    while (::read(read_fd_, buf, sizeof(buf)) > 0) { /* drain */ }
    // Reset AFTER consuming the fd, not before: a producer racing its write
    // between our read and this store leaves the fd readable, so the next
    // poll() returns at once. Cleared first, that racing write would be
    // swallowed by our read while its flag survived — later producers would
    // then skip the write against an EMPTY fd and the loop would sleep a
    // full poll tick with work queued.
    signalled_.store(false, std::memory_order_release);
  }

  void Close() {
    if (write_fd_ >= 0 && write_fd_ != read_fd_) ::close(write_fd_);
    if (read_fd_ >= 0) ::close(read_fd_);
    read_fd_ = write_fd_ = -1;
    signalled_.store(false, std::memory_order_release);  // fresh after reopen
  }

 private:
  int read_fd_ = -1;
  int write_fd_ = -1;
  // True while a wakeup is pending (written fd not yet drained) — the syscall
  // elision above. See Signal()/Drain() for the ordering contract.
  std::atomic<bool> signalled_{false};
};

}  // namespace visio_schema::transport
