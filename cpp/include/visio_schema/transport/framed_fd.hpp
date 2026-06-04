// FramedFdEndpoint — COBS-delimited core-frames over a fixed byte Link.
// Transport-neutral: SerialEndpoint (CDC-ACM) and TcpEndpoint (TCP client) are
// thin subclasses. It does NOT reconnect — a broken link (read EOF or a failed
// write) throws EndpointClosed, and the caller decides what to do.
#pragma once

#include <cstdint>
#include <memory>
#include <vector>

#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/link.hpp"

namespace visio_schema::transport {

class FramedFdEndpoint : public Endpoint {
 public:
  explicit FramedFdEndpoint(std::shared_ptr<Link> link);

  int Fileno() const override { return link_ ? link_->Fileno() : -1; }
  std::vector<Message> TryRead() override;   // throws EndpointClosed on EOF
  void Write(const Message& msg) override;   // throws EndpointClosed on a broken link
  void Close() override;

 private:
  std::shared_ptr<Link> link_;
  std::vector<std::uint8_t> rx_buf_;
};

}  // namespace visio_schema::transport
