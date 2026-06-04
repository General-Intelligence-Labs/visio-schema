// Endpoint — sync read+write interface to one connection.
//
// One ABC; the bus's slot determines whether reads or writes are used:
//   - bus.AttachSource(ep): bus reads via TryRead().
//   - bus.AttachSink(ep):   bus writes via Write(); also polls for control reads.
//
// Endpoints have NO threads and do NOT reconnect: on a broken link they throw
// EndpointClosed, and the caller (the bus, or an app loop) decides what to do.
// Lives in visio-schema so a schema-only user can read/write one stream.
#pragma once

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

  // Send `msg` to the peer. Throws EndpointClosed on a broken link.
  virtual void Write(const Message& msg) = 0;

  // Idempotent shutdown.
  virtual void Close() = 0;
};

}  // namespace visio_schema::transport
