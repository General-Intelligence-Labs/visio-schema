// Endpoint — an ACTIVE OBJECT: one self-contained, self-threaded connection.
//
// Each endpoint owns its concurrency. Start() spawns the endpoint's own I/O
// thread; the endpoint does its own fd polling, outbound queueing (bounded, per
// WritePolicy), blocking writes, blocking reads, and reconnect — none of it on
// the caller's thread. The bus is therefore a thin router with no I/O threads of
// its own: it Send()s to sinks (a thread-safe, non-blocking enqueue) and receives
// inbound via the on_inbound callback the endpoint invokes from its OWN thread.
//
//   Start(on_inbound, on_closed): spawn the I/O thread. on_inbound(msg, this) is
//     called from that thread for each decoded inbound message; on_closed(this)
//     is called once if a FIXED link hits EOF (the owner then detaches it).
//     Reopenable endpoints self-heal and never call on_closed. A write-only sink
//     (e.g. the recorder) ignores both callbacks.
//   Send(msg): thread-safe, non-blocking — enqueue for sending; the endpoint's
//     own thread performs the actual (possibly blocking) write. Sheds per its
//     WritePolicy on a full/stalled link; never blocks the caller.
//   Stop(): stop + join the I/O thread, close the link. Idempotent.
//
// Lives in visio-schema so a schema-only user can run one stream with no bus.
#pragma once

#include <functional>
#include <stdexcept>

#include "visio_schema/wire/message.hpp"

namespace visio_schema::transport {

using visio_schema::wire::Message;

// Thrown by the byte/link layer when a connection breaks (EOF, broken pipe).
// Surfaced to the endpoint's own thread, which either self-heals (reopenable) or
// reports it via on_closed (fixed link).
class EndpointClosed : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

class Endpoint {
 public:
  // Invoked from the endpoint's OWN thread for each decoded inbound message.
  using InboundFn = std::function<void(Message, Endpoint*)>;
  // Invoked from the endpoint's OWN thread once a fixed link hits EOF.
  using ClosedFn = std::function<void(Endpoint*)>;

  virtual ~Endpoint() = default;

  // Spawn the endpoint's I/O thread. Either callback may be empty.
  virtual void Start(InboundFn on_inbound, ClosedFn on_closed) = 0;

  // Thread-safe, non-blocking enqueue for sending (drains on the endpoint's thread).
  virtual void Send(const Message& msg) = 0;

  // Stop + join the I/O thread and close the link. Idempotent.
  virtual void Stop() = 0;
};

}  // namespace visio_schema::transport
