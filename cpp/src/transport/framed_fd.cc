#include "visio_schema/transport/framed_fd.hpp"

#include <poll.h>
#include <unistd.h>

#include "visio_schema/transport/framing.hpp"
#include "visio_schema/wire/time.hpp"  // MonotonicNs

namespace visio_schema::transport {

namespace {
constexpr int kTickMs = 200;  // reopen / watchdog cadence
}  // namespace

void FramedFdEndpoint::AdoptFd(int fd) {
  // A reactor endpoint MUST drive its fd non-blocking: WriteSome/ReadSome run on
  // the I/O thread and a blocking ::write against a stalled peer would freeze it.
  // Refuse an fd whose O_NONBLOCK can't be set.
  if (fd < 0) {
    fd_ = -1;
    return;
  }
  if (!SetNonblocking(fd)) {
    CloseFd(fd);
    fd_ = -1;
    return;
  }
  fd_ = fd;
}

FramedFdEndpoint::FramedFdEndpoint(int fd, WritePolicy policy) : outbox_(policy) {
  AdoptFd(fd);
}

FramedFdEndpoint::FramedFdEndpoint(FdFactory factory, WritePolicy policy,
                                   std::int64_t reopen_backoff_ns)
    : factory_(std::move(factory)),
      outbox_(policy),
      reopen_backoff_ns_(reopen_backoff_ns) {
  if (factory_) AdoptFd(factory_());
}

FramedFdEndpoint::~FramedFdEndpoint() { Stop(); }

void FramedFdEndpoint::Start(InboundFn on_inbound, ClosedFn on_closed) {
  on_inbound_ = std::move(on_inbound);
  on_closed_ = std::move(on_closed);
  wake_.Open();
  stop_.store(false);
  thread_ = std::thread([this] { Loop(); });
}

void FramedFdEndpoint::Stop() {
  stop_.store(true);
  Wake();
  if (thread_.joinable()) thread_.join();
  if (fd_ >= 0) {
    CloseFd(fd_);
    fd_ = -1;
  }
  wake_.Close();
}

void FramedFdEndpoint::Send(const Message& msg) {
  const auto framed = EncodeFramed(msg);
  outbox_.Enqueue(framed.data(), framed.size());  // thread-safe; no I/O
  Wake();
}

void FramedFdEndpoint::Wake() { wake_.Signal(); }

void FramedFdEndpoint::Pump() {
  if (fd_ < 0) return;
  const int fd = fd_;
  const bool alive = outbox_.Drain(
      [fd](const std::uint8_t* p, std::size_t n) { return WriteSome(fd, p, n); });
  if (!alive) MarkLinkDead();
}

void FramedFdEndpoint::MarkLinkDead() {
  if (fd_ >= 0) {
    CloseFd(fd_);
    fd_ = -1;
  }
  outbox_.Clear();  // a fresh reader after reopen would desync on a half-frame
  rx_buf_.clear();
  next_reopen_ns_ = 0;  // reopen ASAP on the next Tick
}

bool FramedFdEndpoint::Reopen() {
  if (!factory_) return false;
  if (const int fresh = factory_(); fresh >= 0) {
    AdoptFd(fresh);
    rx_buf_.clear();
  }
  return link_up_unlocked();
}

void FramedFdEndpoint::Tick(std::int64_t now_ns) {
  if (fd_ >= 0 || !factory_) return;
  if (now_ns < next_reopen_ns_) return;
  if (!Reopen()) next_reopen_ns_ = now_ns + reopen_backoff_ns_;
}

void FramedFdEndpoint::Loop() {
  while (!stop_.load()) {
    const int fd = fd_;
    pollfd pfds[2];
    int n = 0;
    pfds[n++] = {wake_.poll_fd(), POLLIN, 0};
    int fd_idx = -1;
    if (fd >= 0) {
      short ev = POLLIN;
      if (outbox_.HasPending()) ev |= POLLOUT;
      fd_idx = n;
      pfds[n++] = {fd, ev, 0};
    }
    ::poll(pfds, n, kTickMs);
    if (pfds[0].revents & POLLIN) wake_.Drain();
    if (stop_.load()) break;

    Pump();  // drain outbox (no-op if fd down / nothing pending)

    if (fd >= 0 && fd_idx >= 0 &&
        (pfds[fd_idx].revents & (POLLIN | POLLHUP | POLLERR))) {
      if (ReadInbound(fd)) return;  // fixed-fd EOF: on_closed fired, thread exits
    }

    Tick(MonotonicNs());  // reopen / watchdog
  }
}

bool FramedFdEndpoint::ReadInbound(int fd) {
  std::uint8_t chunk[4096];
  const long r = ReadSome(fd, chunk, sizeof(chunk));
  if (r == 0) return false;  // EAGAIN: nothing ready
  if (r < 0) {               // EOF / dead fd
    if (factory_) {
      MarkLinkDead();         // reopenable: self-heal on the next Tick
      return false;
    }
    if (on_closed_) on_closed_(this);  // fixed fd: owner detaches us
    return true;                       // thread exits
  }
  rx_buf_.insert(rx_buf_.end(), chunk, chunk + r);
  for (auto& m : ExtractFrames(rx_buf_)) {
    if (on_inbound_) on_inbound_(std::move(m), this);
  }
  return false;
}

}  // namespace visio_schema::transport
