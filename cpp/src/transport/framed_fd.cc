#include "visio_schema/transport/framed_fd.hpp"

#include <poll.h>
#include <sys/resource.h>
#include <unistd.h>

#include <iostream>

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

}  // namespace

FramedFdEndpoint::FramedFdEndpoint(int fd, WritePolicy policy,
                                   std::int64_t stall_ns)
    : ctrl_outbox_(ControlPolicy()), outbox_(policy), stall_ns_(stall_ns) {
  AdoptFd(fd);
}

FramedFdEndpoint::FramedFdEndpoint(FdFactory factory, WritePolicy policy,
                                   std::int64_t reopen_backoff_ns,
                                   std::int64_t stall_ns)
    : factory_(std::move(factory)),
      ctrl_outbox_(ControlPolicy()),
      outbox_(policy),
      stall_ns_(stall_ns),
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
  // nothing — the reason a thinned preview saves device CPU, not just bandwidth.

  // Nobody is reading: framing bulk or decimatable frames is pure waste — a
  // recovering reader has no use for their stale backlog (video re-syncs at a
  // keyframe; per-sample state is superseded). Everything else — control
  // streams and low-rate data like ButtonEvent or the raw IMU bundles — still
  // enqueues into the bounded control outbox: control is the probe whose
  // first accepted write detects the reader coming back, and one-shot events
  // must survive the stall to be delivered on recovery. The producers decide
  // what is shed-safe by marking it (message.hpp); the on-device MCAP sink is
  // not a framed leg, but a downstream recorder over TCP IS subject to this
  // gate after a >3 s reader wedge — door_dropped() makes that gap visible.
  const bool stalled = link_stalled_.load(std::memory_order_relaxed);
  if (stalled && (msg.bulk || msg.decimatable)) {
    door_dropped_.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  // Absent rule = keep at full rate, so a stream announced after the policy was
  // resolved is delivered rather than silently dropped.
  const StreamRule* rule = RuleFor(msg.stream_id);
  if (rule != nullptr && rule->drop) return;
  // A cap never applies to inter-coded video (stream_policy.hpp).
  if (rule != nullptr && rule->min_gap_us != 0 && !msg.bulk) {
    const std::int64_t now_us = FramedOutbox::SteadyNowUs();
    std::int64_t& last_us = decim_last_us_[msg.stream_id];
    if (now_us - last_us < rule->min_gap_us) return;
    last_us = now_us;
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
  // A stalled link drains nothing, so waking vs_ep_io per message only burns
  // a futex+poll cycle per enqueue (tens/s on a readerless serial leg,
  // forever). The kTickMs idle tick already retries the in-flight probe
  // write, and the first accepted write clears link_stalled_ — so recovery
  // needs no per-message wake either; it costs at most one tick of latency
  // on the first frames after a reader returns.
  if (!stalled) Wake();
}

void FramedFdEndpoint::Wake() { wake_.Signal(); }

void FramedFdEndpoint::Pump() {
  if (fd_ < 0) return;
  // Shed the video queued before the client's last change of mind — a backlog
  // it either no longer wants, or (on a resume) cannot decode and which would
  // only delay the keyframe it is waiting for.
  //
  // Frame boundary only: Clear() mid-write would splice a frame and desync the
  // reader's COBS framing. Clear() is leg-thread-only and Pump runs on the leg
  // thread, so this is the safe place.
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
      std::cerr << "visio-schema: link recovered ("
                << door_dropped_.load(std::memory_order_relaxed)
                << " frames door-dropped while stalled)\n";
      // The reader is back. Whatever bulk survived queuing is stale; flush it
      // so the viewer re-syncs on the next keyframe instead of replaying a
      // dead backlog.
      RequestBulkFlush();
    }
    return;
  }
  if (last_progress_ns_ == 0) {
    last_progress_ns_ = now_ns;  // first pass with pending bytes: arm
  } else if (now_ns - last_progress_ns_ > stall_ns_ &&
             !link_stalled_.load(std::memory_order_relaxed)) {
    link_stalled_.store(true, std::memory_order_relaxed);
    std::cerr << "visio-schema: link stalled (no reader for "
              << stall_ns_ / 1'000'000 << " ms) — shedding bulk/decimatable\n";
  }
}

void FramedFdEndpoint::ReportClosedOnce() {
  if (closed_reported_) return;
  closed_reported_ = true;
  if (on_closed_) on_closed_(this);
}

void FramedFdEndpoint::MarkLinkDead() {
  // A FIXED fd cannot come back: Tick() only reopens when a factory is set, so
  // for an accepted TCP client or a dialed TcpEndpoint this IS the end of the
  // link — and the owner has to hear about it, exactly as it would from a read
  // EOF. Report it here or the endpoint strands: the fd is gone, so it is never
  // polled again and ReadInbound never runs, leaving it attached to the bus
  // forever while the peer is long gone.
  //
  // This path is reached when a WRITE fails before the read side sees the peer
  // leave, which is the common case for a client that aborts (RST) while the
  // device still has video queued to it: Pump() runs before ReadInbound() in the
  // same loop iteration, so the write error wins the race. Measured on an ego
  // over NCM: a client RSTing with ~2 MB queued stranded its link within two
  // attempts, after which the board (one client at a time) refused every later
  // client until the process restarted.
  //
  // A REOPENABLE endpoint (the serial gadget) is the opposite case and must NOT
  // report closed — it self-heals on the next Tick, and its owner would detach a
  // link that is about to come back.
  const bool fixed_link = !factory_;
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
  // Last, with the endpoint's own state already settled: the owner detaches us
  // from inside this call. Once per link — a second MarkLinkDead (fd_ already
  // -1) must not re-report a closure the owner has acted on.
  if (fixed_link) ReportClosedOnce();
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
  // Below-normal: egress to viewers must yield to the producing device's
  // capture/encode pipeline. When the CPU saturates, THIS thread starving is
  // the designed degradation — the outbox stall gate sheds preview frames —
  // whereas a starved encoder sheds recording frames, which is never
  // acceptable. Harmless off-device (readers are not CPU-bound).
  setpriority(PRIO_PROCESS, 0, 5);
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
    // One teardown for both ways a link dies (see MarkLinkDead): a fixed fd
    // must CLOSE here, not just report — reporting alone left the socket in
    // CLOSE_WAIT until the bus's deferred reap got around to Stop(), and a
    // reconnect-storming client piled those up device-side.
    const bool fixed_link = !factory_;
    MarkLinkDead();  // reopenable: self-heal on Tick; fixed: close + report
    return fixed_link;  // fixed fd: on_closed fired, thread exits
  }
  rx_buf_.insert(rx_buf_.end(), chunk, chunk + r);
  for (auto& m : ExtractFrames(rx_buf_)) {
    if (on_inbound_) on_inbound_(std::move(m), this);
  }
  return false;
}

}  // namespace visio_schema::transport
