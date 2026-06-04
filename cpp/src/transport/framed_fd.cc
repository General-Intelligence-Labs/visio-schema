#include "visio_schema/transport/framed_fd.hpp"

#include <iostream>

#include "visio_schema/transport/framing.hpp"

namespace visio_schema::transport {

namespace {
// A reactor endpoint MUST drive its fd non-blocking: WriteSome/ReadSome run on
// the bus thread, and a blocking ::write against a full send buffer (a stalled
// peer) would freeze the whole reactor. Returns false (and the caller drops the
// link) if O_NONBLOCK could not be set — proceeding with a blocking fd is worse
// than no link, since it could wedge the bus.
bool ForceNonblocking(Link* link) {
  if (!link) return true;
  const int fd = link->Fileno();
  if (fd < 0) return true;
  if (SetNonblocking(fd)) return true;
  std::cerr << "FramedFdEndpoint: O_NONBLOCK failed on fd " << fd
            << " — refusing the link (a blocking fd would freeze the bus)\n";
  return false;
}
}  // namespace

FramedFdEndpoint::FramedFdEndpoint(std::shared_ptr<Link> link, WritePolicy policy)
    : link_(std::move(link)), outbox_(policy) {
  if (link_ && !ForceNonblocking(link_.get())) link_.reset();
}

FramedFdEndpoint::FramedFdEndpoint(LinkFactory factory, WritePolicy policy,
                                   std::int64_t reopen_backoff_ns)
    : factory_(std::move(factory)),
      outbox_(policy),
      reopen_backoff_ns_(reopen_backoff_ns) {
  if (factory_) link_ = factory_();  // best-effort initial connect
  if (link_ && !ForceNonblocking(link_.get())) link_.reset();
}

short FramedFdEndpoint::PollEvents() const {
  if (!link_) return 0;  // down — OnTick (timer-driven) handles reconnect
  short ev = POLLIN;
  if (outbox_.HasPending()) ev |= POLLOUT;
  return ev;
}

std::vector<Message> FramedFdEndpoint::TryRead() {
  if (!link_) return {};
  std::uint8_t chunk[4096];
  const long n = link_->ReadSome(chunk, sizeof(chunk));
  if (n == 0) return {};  // EAGAIN — nothing ready (NOT EOF)
  if (n < 0) {            // EOF / dead link
    if (factory_) {       // reopenable: self-heal, don't let the bus detach us
      MarkLinkDead();
      return {};
    }
    throw EndpointClosed("EOF on read");  // fixed link: caller detaches
  }
  rx_buf_.insert(rx_buf_.end(), chunk, chunk + n);
  return ExtractFrames(rx_buf_);
}

void FramedFdEndpoint::Write(const Message& msg) {
  const auto framed = EncodeFramed(msg);
  outbox_.Enqueue(framed.data(), framed.size());
  Pump();  // best-effort immediate drain — low latency on a healthy link
}

void FramedFdEndpoint::OnWritable() { Pump(); }

void FramedFdEndpoint::Pump() {
  if (!link_) return;
  // `lk` outlives this Drain: link_ is only reset by MarkLinkDead() below, after
  // Drain() returns synchronously.
  Link* lk = link_.get();
  const bool alive = outbox_.Drain(
      [lk](const std::uint8_t* p, std::size_t n) { return lk->WriteSome(p, n); });
  if (!alive) MarkLinkDead();
}

void FramedFdEndpoint::MarkLinkDead() {
  if (link_) {
    link_->Close();
    link_.reset();
  }
  // Drop queued bytes: after a reopen the peer is a fresh reader, and a
  // half-written frame would desync its COBS framer.
  outbox_.Clear();
  rx_buf_.clear();
  next_reopen_ns_ = 0;  // attempt reopen on the very next tick
}

void FramedFdEndpoint::OnTick(std::int64_t now_ns) {
  if (link_ || !factory_) return;        // up, or fixed link (no reconnect)
  if (now_ns < next_reopen_ns_) return;  // backoff after a failed attempt
  if (!Reopen()) next_reopen_ns_ = now_ns + reopen_backoff_ns_;
}

bool FramedFdEndpoint::Reopen() {
  if (!factory_) return false;
  if (auto fresh = factory_(); fresh && ForceNonblocking(fresh.get())) {
    link_ = std::move(fresh);
    rx_buf_.clear();
  }  // a failed open / un-settable fd leaves link_ down → retry next tick
  return link_up();
}

void FramedFdEndpoint::Close() {
  if (link_) link_->Close();
  link_.reset();
  factory_ = nullptr;  // explicit close: no more reconnect attempts
}

}  // namespace visio_schema::transport
