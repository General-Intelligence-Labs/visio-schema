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
#include <thread>
#include <vector>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/framed_outbox.hpp"
#include "visio_schema/transport/link.hpp"   // fd I/O helpers + FdFactory
#include "visio_schema/transport/write_policy.hpp"

namespace visio_schema::transport {

class FramedFdEndpoint : public Endpoint {
 public:
  // Fixed fd — no reconnect. Takes ownership of `fd` (-1 = already down).
  explicit FramedFdEndpoint(int fd,
                            WritePolicy policy = WritePolicy::drop_oldest());
  // Reopenable — `factory` is called now and on each reconnect (Tick).
  explicit FramedFdEndpoint(FdFactory factory,
                            WritePolicy policy = WritePolicy::drop_oldest(),
                            std::int64_t reopen_backoff_ns = 500'000'000);
  ~FramedFdEndpoint() override;

  void Start(InboundFn on_inbound, ClosedFn on_closed) override;
  void Send(const Message& msg) override;
  void Stop() override;

  // Diagnostics (thread-safe).
  std::size_t pending_bytes() const { return outbox_.PendingBytes(); }
  std::uint64_t dropped() const { return outbox_.Dropped(); }
  bool link_up() const { return fd_ >= 0; }

 protected:
  // Called from the I/O thread each loop iteration (~kTickMs). Base: reopen a
  // down fd with backoff. SerialEndpoint overrides to drive the watchdog.
  virtual void Tick(std::int64_t now_ns);

  bool link_up_unlocked() const { return fd_ >= 0; }
  std::size_t outbox_pending() const { return outbox_.PendingBytes(); }
  void MarkLinkDead();   // I/O thread: close fd + clear outbox; Tick reopens
  bool Reopen();         // I/O thread: fresh fd via factory; returns link_up
  FdFactory factory_;    // null = fixed fd

 private:
  void Loop();           // the I/O thread body
  void Pump();           // drain outbox to the fd (I/O thread)
  bool ReadInbound(int fd);  // read+decode; returns true if the thread should exit
  void Wake();           // poke the I/O thread (from Send/Stop)
  void AdoptFd(int fd);  // O_NONBLOCK-or-close a freshly opened fd into fd_

  int fd_ = -1;                    // I/O-thread-owned after Start
  FramedOutbox outbox_;            // thread-safe (Send enqueues, I/O thread drains)
  std::int64_t reopen_backoff_ns_ = 0;
  std::int64_t next_reopen_ns_ = 0;
  std::vector<std::uint8_t> rx_buf_;
  int wake_fd_ = -1;
  std::thread thread_;
  std::atomic<bool> stop_{false};
  InboundFn on_inbound_;
  ClosedFn on_closed_;
};

}  // namespace visio_schema::transport
