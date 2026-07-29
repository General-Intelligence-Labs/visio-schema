// FramedFdEndpoint — COBS-delimited core-frames over a raw fd, as a self-threaded
// ACTIVE OBJECT. Start() spawns one I/O thread that polls the fd, drains the
// bounded outbox (non-blocking writes), reads + decodes inbound frames (delivered
// via on_inbound), and reopens the fd (Tick). Send() is a thread-safe, non-
// blocking enqueue. Transport-neutral: SerialEndpoint and TcpEndpoint are thin
// subclasses (SerialEndpoint overrides Tick() to run the watchdog).
//
//   - fixed fd (factory null): a read EOF/dead fd reports on_closed(this) once and
//     the I/O thread exits; the owner detaches it.
//   - reopenable (factory set): a dead fd is dropped and Tick() reopens it with
//     backoff. Self-heals; never calls on_closed.
#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/framed_outbox.hpp"
#include "visio_schema/transport/link.hpp"   // fd I/O helpers + FdFactory
#include "visio_schema/transport/wake_fd.hpp"  // pollable cross-thread wakeup
#include "visio_schema/transport/write_policy.hpp"

namespace visio_schema::transport {

class FramedFdEndpoint : public Endpoint {
 public:
  // No-progress window before stalled() latches. 3 s: long enough that a
  // congested-but-alive Wi-Fi link (which still accepts something every few
  // hundred ms) never trips it. Constructor-injected (like reopen_backoff_ns)
  // so tests can shrink it; immutable after construction.
  static constexpr std::int64_t kDefaultStallNs = 3'000'000'000;

  // Fixed fd — no reconnect. Takes ownership of `fd` (-1 = already down).
  explicit FramedFdEndpoint(int fd,
                            WritePolicy policy = WritePolicy::drop_oldest(),
                            std::int64_t stall_ns = kDefaultStallNs);
  // Reopenable — `factory` is called now and on each reconnect (Tick).
  explicit FramedFdEndpoint(FdFactory factory,
                            WritePolicy policy = WritePolicy::drop_oldest(),
                            std::int64_t reopen_backoff_ns = 500'000'000,
                            std::int64_t stall_ns = kDefaultStallNs);
  ~FramedFdEndpoint() override;

  void Start(InboundFn on_inbound, ClosedFn on_closed) override;
  void Send(const Message& msg) override;
  void Stop() override;

  // This client's delivery filter (endpoint.hpp). Replaced whole rather than
  // mutated, so a reader works from a consistent table. Serialized with Send()
  // by the bus dispatch mutex — the same contract decim_last_us_ relies on, and
  // the reason this needs no atomic (libstdc++'s atomic shared_ptr ops take a
  // process-global pthread mutex, which would be ~100x this gate's cost).
  void SetStreamPolicy(
      std::shared_ptr<const ResolvedStreamPolicy> policy) override {
    policy_ = std::move(policy);
  }

  // Honoured by Pump() at a frame boundary (never mid-write, or the reader
  // desyncs on a half-written COBS frame).
  void RequestBulkFlush() override {
    bulk_flush_.store(true, std::memory_order_relaxed);
    Wake();
  }

  // Diagnostics (thread-safe).
  std::size_t pending_bytes() const {
    return ctrl_outbox_.PendingBytes() + outbox_.PendingBytes();
  }
  std::uint64_t dropped() const { return ctrl_outbox_.Dropped() + outbox_.Dropped(); }
  // Frames refused at Send's stalled-gate, before any framing or enqueue.
  // Separate from dropped(): those were queued then shed; these never entered.
  std::uint64_t door_dropped() const {
    return door_dropped_.load(std::memory_order_relaxed);
  }
  bool link_up() const { return fd_ >= 0; }
  // True while no bytes are being accepted with bytes pending (see
  // UpdateStallState) — i.e. nobody is reading this leg. Producers may use it
  // to skip building payloads that Send() would only drop at the door.
  bool stalled() const { return link_stalled_.load(std::memory_order_relaxed); }

 protected:
  // Called from the I/O thread each loop iteration (~kTickMs). Base: reopen a
  // down fd with backoff. SerialEndpoint overrides to drive the watchdog.
  virtual void Tick(std::int64_t now_ns);

  bool link_up_unlocked() const { return fd_ >= 0; }
  std::size_t outbox_pending() const {
    return ctrl_outbox_.PendingBytes() + outbox_.PendingBytes();
  }
  void MarkLinkDead();   // I/O thread: close fd + clear outbox; Tick reopens
  bool Reopen();         // I/O thread: fresh fd via factory; returns link_up
  FdFactory factory_;    // null = fixed fd

 private:
  void Loop();           // the I/O thread body
  void Pump();           // drain outbox to the fd (I/O thread)
  // Stall bookkeeping after each Pump: `accepted` is the bytes the fd took
  // this pass. With bytes pending and none accepted for stall_ns_ the link is
  // STALLED (a serial gadget nobody reads, a peer gone silent): Send then
  // drops bulk + decimatable frames at the door so the CPU stops framing
  // data whose stale backlog is worthless to a recovering reader. Everything
  // else — control streams and one-shot data like ButtonEvent — still
  // enqueues into the bounded control outbox: control is the probe that
  // detects the reader coming back, and the events must survive the stall to
  // be delivered on recovery. When the gate lifts, the bulk backlog is
  // flushed for a clean keyframe re-sync.
  void UpdateStallState(long accepted);
  bool ReadInbound(int fd);  // read+decode; returns true if the thread should exit
  void Wake();           // poke the I/O thread (from Send/Stop)
  void AdoptFd(int fd);  // O_NONBLOCK-or-close a freshly opened fd into fd_

  int fd_ = -1;                    // I/O-thread-owned after Start
  // Split outbox over the single fd: CONTROL frames (command results, DeviceInfo,
  // OTA status, IMU) on ctrl_outbox_ are drained ahead of camera video on
  // outbox_, so a reply never waits behind buffered H.265. Pump() switches
  // between them only at a frame boundary (never mid-frame → no COBS desync).
  FramedOutbox ctrl_outbox_;       // non-bulk: control/sensor; near-lossless
  FramedOutbox outbox_;            // bulk: camera video; lossy backpressure
  std::atomic<bool> bulk_flush_{false};     // shed queued video at a frame boundary
  std::atomic<bool> link_stalled_{false};   // set by I/O thread, read in Send
  std::atomic<std::uint64_t> door_dropped_{0};  // refused at the stalled gate
  std::int64_t last_progress_ns_ = 0;       // I/O-thread-private
  const std::int64_t stall_ns_;             // see kDefaultStallNs
  // This client's filter; null = deliver everything. Guarded by the bus dispatch
  // serialization, like decim_last_us_ below.
  std::shared_ptr<const ResolvedStreamPolicy> policy_;
  // The rule for `stream_id`, or null when nothing restricts it.
  const StreamRule* RuleFor(std::uint32_t stream_id) const {
    if (!policy_) return nullptr;
    const auto it = policy_->find(stream_id);
    return it == policy_->end() ? nullptr : &it->second;
  }
  // Last-forwarded time per rate-capped stream. Touched only from Send, which
  // the bus serializes under its dispatch mutex (every send path — Publish,
  // Relay, ReplyTo — takes it), so this needs no lock of its own. Bounded by the
  // number of channels a rule matched.
  std::unordered_map<std::uint32_t, std::int64_t> decim_last_us_;
  std::int64_t reopen_backoff_ns_ = 0;
  std::int64_t next_reopen_ns_ = 0;
  std::vector<std::uint8_t> rx_buf_;
  WakeFd wake_;
  std::thread thread_;
  std::atomic<bool> stop_{false};
  InboundFn on_inbound_;
  ClosedFn on_closed_;
  // A fixed link dies once, but two paths can notice — a read EOF and a write
  // failure. One helper so a third can't forget the latch.
  void ReportClosedOnce();
  bool closed_reported_ = false;
};

}  // namespace visio_schema::transport
