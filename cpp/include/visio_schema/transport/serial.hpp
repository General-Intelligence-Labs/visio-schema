// SerialEndpoint — COBS-delimited core-frames over a CDC-ACM / serial Link.
// A thin alias of FramedFdEndpoint (all framing lives there); kept as its own
// type so call sites read intent-fully ("the serial leg"). Fixed-link, no
// reconnect: a broken link throws EndpointClosed.
#pragma once

#include "visio_schema/transport/framed_fd.hpp"

namespace visio_schema::transport {

class SerialEndpoint : public FramedFdEndpoint {
 public:
  using FramedFdEndpoint::FramedFdEndpoint;  // fixed-link ctor
};

}  // namespace visio_schema::transport
