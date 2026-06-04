// Endpoint — sync read+write interface to one connection.
//
// One ABC; the bus's slot determines whether reads or writes are used:
//   - bus.AttachSource(ep): bus reads via TryRead().
//   - bus.AttachSink(ep):   bus writes via Write(); also polls for control reads.
//
// Endpoints have NO threads. A fixed-link endpoint does not reconnect: on a
// broken link it throws EndpointClosed and the caller (the bus, or an app loop)
// decides what to do. A reactor endpoint (FramedFdEndpoint with a LinkFactory)
// instead self-heals — it buffers writes in a bounded outbox, drains them when
// the bus reports the fd writable (OnWritable), and reopens the link on a timer
// tick (OnTick) — so it never blocks the bus thread and never throws on a
// transient stall. Lives in visio-schema so a schema-only user can read/write
// one stream.
#pragma once

#include <poll.h>

#include <cstdint>
#include <stdexcept>
#include <vector>

#include "visio_schema/wire/message.hpp"

namespace visio_schema::transport {

using visio_schema::wire::Message;

// Thrown by an Endpoint/Link when its connection breaks (EOF, broken pipe).
// Endpoints never reconnect themselves — they throw this so the caller decides
// (the bus detaches + deregisters the endpoint; an app may dial a fresh link).
class EndpointClosed : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

class Endpoint {
 public:
  virtual ~Endpoint() = default;

  // Return the fd the bus's poll() should monitor for readable events, or -1 if
  // this endpoint isn't fd-driven.
  virtual int Fileno() const = 0;

  // Called when Fileno() is readable. Returns zero or more decoded Messages.
  // Throws EndpointClosed on EOF / a broken link.
  virtual std::vector<Message> TryRead() = 0;

  // Send `msg` to the peer. A reactor sink enqueues into its outbox and never
  // blocks/throws on a full or stalled link (it sheds per its WritePolicy); a
  // fixed-link endpoint may throw EndpointClosed on a broken link.
  virtual void Write(const Message& msg) = 0;

  // Idempotent shutdown.
  virtual void Close() = 0;

  // ── Reactor hooks (default no-op; only fd reactor sinks override) ──────
  // The poll() event mask the bus should request for Fileno(). Default: POLLIN
  // when fd-driven. A sink with queued outbound bytes also requests POLLOUT so
  // the loop calls OnWritable() once the kernel send buffer drains.
  virtual short PollEvents() const { return Fileno() >= 0 ? POLLIN : 0; }

  // Called when Fileno() reports POLLOUT — drain the outbox (non-blocking).
  virtual void OnWritable() {}

  // Called periodically on the bus thread (~2 Hz). Drives link reopen + the
  // serial liveness watchdog. `now_ns` is MonotonicNs().
  virtual void OnTick(std::int64_t now_ns) { (void)now_ns; }
};

}  // namespace visio_schema::transport
