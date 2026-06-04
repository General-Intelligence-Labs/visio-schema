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
  std::vector<std::uint8_t> decoded;  // reused scratch across frames
  // Advance a cursor and decode each COBS run straight from rx_buf (no per-frame
  // copy), then erase all consumed bytes ONCE — O(n) instead of an O(remaining)
  // shift per frame.
  std::size_t pos = 0;
  while (pos < rx_buf.size()) {
    auto begin = rx_buf.begin() + pos;
    auto it = std::find(begin, rx_buf.end(), std::uint8_t{0});
    if (it == rx_buf.end()) break;  // partial trailing frame: keep it for next read
    const std::size_t delim = static_cast<std::size_t>(it - rx_buf.begin());
    const std::size_t len = delim - pos;
    if (len == 0) {  // bare 0x00; skip empty frame
      pos = delim + 1;
      continue;
    }
    std::string_view enc_view{
        reinterpret_cast<const char*>(rx_buf.data() + pos), len};
    pos = delim + 1;
    decoded.clear();
    if (!visio_schema::wire::CobsDecode(enc_view, &decoded)) {
      std::cerr << "visio-schema: COBS decode failed (" << len << " bytes)\n";
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
  rx_buf.erase(rx_buf.begin(), rx_buf.begin() + pos);
  return out;
}

std::vector<std::uint8_t> EncodeFramed(const Message& msg) {
  const std::string frame = visio_schema::wire::EncodeFrame(msg);
  auto encoded = visio_schema::wire::CobsEncode(std::string_view{frame});
  encoded.push_back(0);
  return encoded;
}

bool WriteFramed(Link& link, const Message& msg) {
  const auto out = EncodeFramed(msg);
  return link.Write({reinterpret_cast<const char*>(out.data()), out.size()});
}

}  // namespace visio_schema::transport
