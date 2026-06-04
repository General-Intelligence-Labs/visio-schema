#include "visio_schema/transport/mcap_endpoint.hpp"

#include <utility>

namespace visio_schema::transport {

McapEndpoint::McapEndpoint(std::string_view path, StreamResolver resolve,
                           std::uint64_t max_bytes, double max_duration_s)
    : resolve_(std::move(resolve)),
      writer_(std::make_unique<visio_schema::mcap::McapWriter>(
          path, max_bytes, max_duration_s)) {}

McapEndpoint::~McapEndpoint() { Close(); }

void McapEndpoint::Write(const Message& msg) {
  if (closed_) return;
  // Drop until the stream's DeviceInfo announce has been processed.
  const Channel* ch = resolve_ ? resolve_(msg.stream_id) : nullptr;
  if (ch == nullptr) return;
  writer_->Write(*ch, msg);
}

void McapEndpoint::Close() {
  if (closed_) return;
  closed_ = true;
  if (writer_) writer_->Close();
}

}  // namespace visio_schema::transport
