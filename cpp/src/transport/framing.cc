#include "visio_schema/transport/framing.hpp"

#include <algorithm>
#include <iostream>
#include <string>
#include <string_view>

#include "visio_schema/wire/codec/cobs.hpp"
#include "visio_schema/wire/codec/frame.hpp"

namespace visio_schema::transport {

std::vector<Message> ExtractFrames(std::vector<std::uint8_t>& rx_buf) {
  std::vector<Message> out;
  while (true) {
    auto it = std::find(rx_buf.begin(), rx_buf.end(), std::uint8_t{0});
    if (it == rx_buf.end()) break;
    std::size_t delim = static_cast<std::size_t>(it - rx_buf.begin());
    if (delim == 0) {
      rx_buf.erase(rx_buf.begin());  // bare 0x00; skip empty frame
      continue;
    }
    std::vector<std::uint8_t> encoded(rx_buf.begin(), rx_buf.begin() + delim);
    rx_buf.erase(rx_buf.begin(), rx_buf.begin() + delim + 1);
    std::vector<std::uint8_t> decoded;
    decoded.reserve(encoded.size());
    std::string_view enc_view{reinterpret_cast<const char*>(encoded.data()),
                              encoded.size()};
    if (!visio_schema::wire::CobsDecode(enc_view, &decoded)) {
      std::cerr << "visio-schema: COBS decode failed (" << encoded.size()
                << " bytes)\n";
      continue;
    }
    Message msg;
    std::string_view frame_view{reinterpret_cast<const char*>(decoded.data()),
                                decoded.size()};
    const auto status = visio_schema::wire::DecodeFrame(frame_view, &msg);
    if (status != visio_schema::wire::FrameStatus::kOk) {
      std::cerr << "visio-schema: frame decode dropped: "
                << visio_schema::wire::FrameStatusName(status) << "\n";
      continue;
    }
    out.push_back(std::move(msg));
  }
  return out;
}

bool WriteFramed(Link& link, const Message& msg) {
  const std::string frame = visio_schema::wire::EncodeFrame(msg);
  auto encoded = visio_schema::wire::CobsEncode(std::string_view{frame});
  encoded.push_back(0);
  return link.Write(
      {reinterpret_cast<const char*>(encoded.data()), encoded.size()});
}

}  // namespace visio_schema::transport
