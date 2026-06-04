// FramedFdEndpoint — COBS-delimited core-frames over a byte Link, as a passive
// reactor sink. Transport-neutral: SerialEndpoint (CDC-ACM) and TcpEndpoint (TCP
// client) are thin subclasses.
//
// Write() never blocks and never throws on a full/stalled link: it frames the
// message and enqueues it into a bounded FramedOutbox (which sheds per its
// WritePolicy). The bus drains the outbox via OnWritable() when the fd is
// writable, so a slow consumer applies backpressure (oldest frames drop) instead
// of stalling the bus thread.
//
// Reconnect is opt-in via a LinkFactory:
//   - fixed link (factory null): a read EOF / dead link throws EndpointClosed,
//     so the bus detaches the endpoint (sources behave as before).
//   - reopenable (factory set): a dead link is dropped silently; OnTick() reopens
//     it with backoff. The endpoint self-heals and is never detached.
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <vector>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/framed_outbox.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/write_policy.hpp"

namespace visio_schema::transport {

class FramedFdEndpoint : public Endpoint {
 public:
  // Returns a freshly opened link, or nullptr if (re)connect failed this tick.
  using LinkFactory = std::function<std::shared_ptr<Link>()>;

  // Fixed link — no reconnect. A broken link throws EndpointClosed on read.
  explicit FramedFdEndpoint(std::shared_ptr<Link> link,
                            WritePolicy policy = WritePolicy::drop_oldest());

  // Reopenable — `factory` is called now (best-effort; may return nullptr) and
  // again on each reconnect from OnTick(), no sooner than reopen_backoff_ns
  // after a failed attempt.
  explicit FramedFdEndpoint(LinkFactory factory,
                            WritePolicy policy = WritePolicy::drop_oldest(),
                            std::int64_t reopen_backoff_ns = 500'000'000);

  int Fileno() const override { return link_ ? link_->Fileno() : -1; }
  short PollEvents() const override;
  std::vector<Message> TryRead() override;
  void Write(const Message& msg) override;   // enqueue; never blocks/throws-on-full
  void OnWritable() override;
  void OnTick(std::int64_t now_ns) override;
  void Close() override;

  // Bytes the outbox is holding — the serial watchdog's "is the host draining
  // us" signal. Zero while the link keeps up.
  std::size_t pending_bytes() const { return outbox_.PendingBytes(); }
  bool link_up() const { return static_cast<bool>(link_); }

 protected:
  void Pump();          // best-effort non-blocking drain of the outbox
  void MarkLinkDead();  // drop the link; reopenable endpoints retry in OnTick
  bool Reopen();        // force a fresh open via the factory; returns link_up()

  std::shared_ptr<Link> link_;
  LinkFactory factory_;            // null = fixed link (no reconnect)
  FramedOutbox outbox_;
  std::int64_t reopen_backoff_ns_ = 0;
  std::int64_t next_reopen_ns_ = 0;
  std::vector<std::uint8_t> rx_buf_;
};

}  // namespace visio_schema::transport
