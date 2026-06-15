#include "visio_schema/transport/framed_fd.hpp"

#include <poll.h>
#include <sys/eventfd.h>
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

// Control queue: near-lossless and bounded by frame count (control + IMU are
// low-byte; 512 frames is generous headroom, dropping oldest only if the link
// stalls for seconds). OneAtATime drain (WritePolicy default) so Pump can
// interleave it with video at frame boundaries.
namespace {
WritePolicy ControlPolicy() { return WritePolicy::drop_oldest(512); }
}  // namespace

FramedFdEndpoint::FramedFdEndpoint(int fd, WritePolicy policy)
    : ctrl_outbox_(ControlPolicy()), outbox_(policy) {
  AdoptFd(fd);
}

FramedFdEndpoint::FramedFdEndpoint(FdFactory factory, WritePolicy policy,
                                   std::int64_t reopen_backoff_ns)
    : factory_(std::move(factory)),
      ctrl_outbox_(ControlPolicy()),
      outbox_(policy),
      reopen_backoff_ns_(reopen_backoff_ns) {
  if (factory_) AdoptFd(factory_());
}

FramedFdEndpoint::~FramedFdEndpoint() { Stop(); }

void FramedFdEndpoint::Start(InboundFn on_inbound, ClosedFn on_closed) {
  on_inbound_ = std::move(on_inbound);
  on_closed_ = std::move(on_closed);
  if (wake_fd_ < 0) wake_fd_ = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
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
  if (wake_fd_ >= 0) {
    ::close(wake_fd_);
    wake_fd_ = -1;
  }
}

void FramedFdEndpoint::Send(const Message& msg) {
  // Video paused for this client (e.g. the app is on a non-video screen): drop
  // it at the door so it never enters the queue or contends for the AP. Control
  // is never paused. Set under the same dispatch serialization as Send, so the
  // relaxed read is consistent here.
  if (msg.bulk && bulk_paused_.load(std::memory_order_relaxed)) return;
  const auto framed = EncodeFramed(msg);
  // Bulk (camera video) -> lossy video queue; everything else -> the control
  // queue, which Pump() drains ahead of video. thread-safe; no I/O.
  (msg.bulk ? outbox_ : ctrl_outbox_).Enqueue(framed.data(), framed.size());
  Wake();
}

void FramedFdEndpoint::Wake() {
  if (wake_fd_ < 0) return;
  const std::uint64_t one = 1;
  (void)::write(wake_fd_, &one, sizeof(one));
}

void FramedFdEndpoint::Pump() {
  if (fd_ < 0) return;
  // When streaming was just paused, shed any video still queued from before the
  // pause — but only at a frame boundary (not mid-write, or the reader desyncs).
  // Clear() is leg-thread-only, and Pump runs on the leg thread, so this is the
  // safe place to do it. Frees the AP within one drain cycle.
  if (bulk_paused_.load(std::memory_order_relaxed) && !outbox_.InFlightActive())
    outbox_.Clear();
  const int fd = fd_;
  const auto wr = [fd](const std::uint8_t* p, std::size_t n) {
    return WriteSome(fd, p, n);
  };
  // Multiplex the two outboxes over the one fd WITHOUT splitting a frame: if
  // either has a frame mid-write (bytes already on the wire), finish exactly
  // that one — switching now would inject the other queue's bytes into a
  // half-written COBS frame and desync the reader. Only at a frame boundary
  // (neither in-flight) do we choose, and then control goes first so a reply
  // never waits behind the video backlog. The video outbox is OneAtATime, so
  // "finish the in-flight frame" is bounded to a single video frame.
  FramedOutbox* pick = outbox_.InFlightActive()        ? &outbox_
                       : ctrl_outbox_.InFlightActive()  ? &ctrl_outbox_
                       : ctrl_outbox_.HasPending()      ? &ctrl_outbox_
                                                        : &outbox_;
  if (!pick->Drain(wr)) MarkLinkDead();
}

void FramedFdEndpoint::MarkLinkDead() {
  if (fd_ >= 0) {
    CloseFd(fd_);
    fd_ = -1;
  }
  ctrl_outbox_.Clear();  // a fresh reader after reopen would desync on a half-frame
  outbox_.Clear();
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
    pfds[n++] = {wake_fd_, POLLIN, 0};
    int fd_idx = -1;
    if (fd >= 0) {
      short ev = POLLIN;
      if (ctrl_outbox_.HasPending() || outbox_.HasPending()) ev |= POLLOUT;
      fd_idx = n;
      pfds[n++] = {fd, ev, 0};
    }
    ::poll(pfds, n, kTickMs);
    if (pfds[0].revents & POLLIN) {
      std::uint64_t drain;
      while (::read(wake_fd_, &drain, sizeof(drain)) > 0) { /* drain */ }
    }
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
