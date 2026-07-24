#include "visio_schema/transport/framed_fd.hpp"

#include <poll.h>
#include <unistd.h>

#include "visio_schema/transport/framing.hpp"
#include "visio_schema/transport/link.hpp"  // SetCurrentThreadName
#include "visio_schema/wire/time.hpp"       // MonotonicNs

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
// stalls for seconds). BatchAll drain: at ~470 IMU messages/s, OneAtATime
// made every tiny frame its own write() and — with TCP_NODELAY — its own
// packet, ~500 syscalls+packets/s per client. Coalescing costs one copy of a
// few KB per drain pass and still interleaves with video at frame boundaries
// (the batch is one in-flight unit; worst case a video frame waits behind
// ~25 KB of control, microseconds on any link this serves).
namespace {
WritePolicy ControlPolicy() {
  WritePolicy p = WritePolicy::drop_oldest(512);
  p.drain = WritePolicy::DrainMode::BatchAll;
  return p;
}

// No bytes accepted for this long WITH bytes pending = the link is stalled
// (serial gadget nobody reads, peer gone silent). Long enough that a
// congested-but-alive Wi-Fi link (which still accepts something every few
// hundred ms) never trips it.
constexpr std::int64_t kStallNs = 3'000'000'000;
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
  // Door checks BEFORE any framing work, so a frame nobody will get costs
  // nothing. All flags are set under the same dispatch serialization as Send
  // (or by this endpoint's own I/O thread), so relaxed reads are consistent.
  const bool stalled = link_stalled_.load(std::memory_order_relaxed);
  // Video paused for this client (app on a non-video screen), or the link is
  // stalled: drop bulk at the door so it never enters the queue. Control is
  // never paused.
  if (msg.bulk && (bulk_paused_.load(std::memory_order_relaxed) || stalled))
    return;
  if (msg.decimatable) {
    // On a stalled link derived per-sample streams are pure waste — the raw
    // bundles carry the ground truth and nothing is delivered anyway.
    if (stalled) return;
    // Client-requested live-rate cap (SetImuLiveRate), per stream.
    if (const int rate = live_rate_hz_.load(std::memory_order_relaxed)) {
      const std::int64_t min_gap_us = 1'000'000 / rate;
      const std::int64_t now_us = FramedOutbox::SteadyNowUs();
      std::int64_t& last_us = decim_last_us_[msg.stream_id];
      if (now_us - last_us < min_gap_us) return;
      last_us = now_us;
    }
  }
  // Frame ONCE per message: the framed bytes are identical for every sink
  // (see wire::Message::framed), so whoever gets here first pays the
  // COBS+CRC pass and the rest just take a refcount.
  if (!msg.framed) {
    msg.framed = std::make_shared<const std::vector<std::uint8_t>>(
        EncodeFramed(msg));
  }
  // Bulk (camera video) -> lossy video queue; everything else -> the control
  // queue, which Pump() drains ahead of video. thread-safe; no I/O.
  (msg.bulk ? outbox_ : ctrl_outbox_).Enqueue(msg.framed, msg.keyframe);
  Wake();
}

void FramedFdEndpoint::Wake() { wake_.Signal(); }

void FramedFdEndpoint::Pump() {
  if (fd_ < 0) return;
  // When streaming was just paused, shed any video still queued from before the
  // pause — but only at a frame boundary (not mid-write, or the reader desyncs).
  // Clear() is leg-thread-only, and Pump runs on the leg thread, so this is the
  // safe place to do it. Frees the AP within one drain cycle.
  if (bulk_paused_.load(std::memory_order_relaxed) && !outbox_.InFlightActive())
    outbox_.Clear();
  // A viewer just (re)started decoding: drop the video it queued before that
  // moment so the keyframe it is waiting for is next on the wire instead of a
  // second deep. Frame boundary only — Clear() mid-write would splice a frame.
  if (bulk_flush_.load(std::memory_order_relaxed) && !outbox_.InFlightActive()) {
    outbox_.Clear();
    bulk_flush_.store(false, std::memory_order_relaxed);
  }
  const int fd = fd_;
  long accepted = 0;  // bytes the fd took this pass — feeds stall detection
  const auto wr = [fd, &accepted](const std::uint8_t* p, std::size_t n) {
    const long r = WriteSome(fd, p, n);
    if (r > 0) accepted += r;
    return r;
  };
  // Keep draining while the link keeps accepting. A OneAtATime Drain() promotes
  // exactly ONE frame, so a single Drain() per poll wakeup caps this leg at one
  // frame per wakeup. That is invisible on a fast link (writes never EAGAIN, so
  // poll returns immediately and the loop spins), but on a real one it throttles
  // the leg to the POLLOUT rate — and this device publishes ~550 messages/s
  // (~60 video + ~467 IMU + audio), so the backlog grows, frames age past the
  // outbox's max_age and are evicted. Measured symptom: the kernel send queue
  // sat EMPTY in 94 of 100 samples while the viewer saw 0.4-0.6 s gaps and its
  // decoder lost sync — we simply weren't feeding the socket.
  //
  // Each iteration re-picks, so control frames still interleave at frame
  // boundaries; the loop stops the moment a write reports EAGAIN (bytes left
  // in flight) or nothing is pending. Bounded so a saturating producer can't
  // starve this thread's inbound reads.
  constexpr int kMaxFramesPerPump = 64;
  for (int i = 0; i < kMaxFramesPerPump; ++i) {
    // Multiplex the two outboxes over the one fd WITHOUT splitting a frame: if
    // either has a frame mid-write (bytes already on the wire), finish exactly
    // that one — switching now would inject the other queue's bytes into a
    // half-written COBS frame and desync the reader. Only at a frame boundary
    // (neither in-flight) do we choose, and then control goes first so a reply
    // never waits behind the video backlog. The video outbox is OneAtATime, so
    // "finish the in-flight frame" is bounded to a single video frame.
    FramedOutbox* pick = outbox_.InFlightActive()         ? &outbox_
                         : ctrl_outbox_.InFlightActive()  ? &ctrl_outbox_
                         : ctrl_outbox_.HasPending()      ? &ctrl_outbox_
                                                          : &outbox_;
    if (!pick->Drain(wr)) {
      MarkLinkDead();
      return;
    }
    // Bytes still in flight => the write hit EAGAIN; wait for the next POLLOUT
    // rather than spinning on a socket that isn't taking data.
    if (pick->InFlightActive()) break;
    if (!ctrl_outbox_.HasPending() && !outbox_.HasPending()) break;
  }
  UpdateStallState(accepted);
}

void FramedFdEndpoint::UpdateStallState(long accepted) {
  const std::int64_t now_ns = MonotonicNs();
  const bool pending = ctrl_outbox_.HasPending() || outbox_.HasPending();
  if (accepted > 0 || !pending) {
    last_progress_ns_ = now_ns;
    if (link_stalled_.load(std::memory_order_relaxed) && accepted > 0) {
      link_stalled_.store(false, std::memory_order_relaxed);
      // The reader is back. Whatever bulk survived queuing is stale; flush it
      // so the viewer re-syncs on the next keyframe instead of replaying a
      // dead backlog.
      RequestBulkFlush();
    }
    return;
  }
  if (last_progress_ns_ == 0) {
    last_progress_ns_ = now_ns;  // first pass with pending bytes: arm
  } else if (now_ns - last_progress_ns_ > kStallNs) {
    link_stalled_.store(true, std::memory_order_relaxed);
  }
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
  // A fresh link starts unstalled and re-arms its own stall clock.
  link_stalled_.store(false, std::memory_order_relaxed);
  last_progress_ns_ = 0;
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
  SetCurrentThreadName("vs_ep_io");
  while (!stop_.load()) {
    const int fd = fd_;
    pollfd pfds[2];
    int n = 0;
    pfds[n++] = {wake_.poll_fd(), POLLIN, 0};
    int fd_idx = -1;
    if (fd >= 0) {
      short ev = POLLIN;
      if (ctrl_outbox_.HasPending() || outbox_.HasPending()) ev |= POLLOUT;
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
