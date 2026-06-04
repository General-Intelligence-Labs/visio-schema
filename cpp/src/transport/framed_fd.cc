#include "visio_schema/transport/framed_fd.hpp"

#include "visio_schema/transport/framing.hpp"

namespace visio_schema::transport {

FramedFdEndpoint::FramedFdEndpoint(std::shared_ptr<Link> link)
    : link_(std::move(link)) {}

std::vector<Message> FramedFdEndpoint::TryRead() {
  if (!link_) return {};   // already closed by the caller
  std::uint8_t chunk[4096];
  const std::size_t n = link_->ReadNonblocking(chunk, sizeof(chunk));
  if (n == 0) throw EndpointClosed("EOF on read");   // the link won't reopen itself
  rx_buf_.insert(rx_buf_.end(), chunk, chunk + n);
  return ExtractFrames(rx_buf_);
}

void FramedFdEndpoint::Write(const Message& msg) {
  if (!link_ || !WriteFramed(*link_, msg)) {
    throw EndpointClosed("write failed");
  }
}

void FramedFdEndpoint::Close() {
  if (link_) link_->Close();
  link_.reset();
}

}  // namespace visio_schema::transport
