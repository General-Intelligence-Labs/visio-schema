#include "visio/wire/codec/frame.hpp"

#include <limits>
#include <stdexcept>

#include "visio/wire/codec/crc16.hpp"

namespace visio::wire {

namespace {

void PackU16LE(std::string* dst, std::uint16_t v) {
  dst->push_back(static_cast<char>(v & 0xFF));
  dst->push_back(static_cast<char>((v >> 8) & 0xFF));
}

std::uint16_t ReadU16LE(const char* p) {
  return static_cast<std::uint16_t>(
      (static_cast<std::uint8_t>(p[0])) |
      (static_cast<std::uint8_t>(p[1]) << 8));
}

}  // namespace

const char* FrameStatusName(FrameStatus s) noexcept {
  switch (s) {
    case FrameStatus::kOk: return "ok";
    case FrameStatus::kFrameTooShort: return "frame_too_short";
    case FrameStatus::kHeaderLenOverflow: return "header_len_overflow";
    case FrameStatus::kCrcMismatch: return "crc_mismatch";
    case FrameStatus::kHeaderParseError: return "header_parse_error";
  }
  return "unknown";
}

std::string EncodeFrame(const visio::wire::v1::Header& header,
                        std::string_view payload) {
  const std::string hbytes = header.SerializeAsString();
  if (hbytes.size() > std::numeric_limits<std::uint8_t>::max()) {
    throw std::length_error("Header too large for u8 HEADER_LEN");
  }
  std::string out;
  out.reserve(1 + hbytes.size() + payload.size() + 2);
  out.push_back(static_cast<char>(hbytes.size()));  // HEADER_LEN (u8)
  out.append(hbytes);
  out.append(payload);
  const std::uint16_t crc = Crc16(out.data(), out.size());
  PackU16LE(&out, crc);
  return out;
}

FrameStatus DecodeFrame(std::string_view frame,
                        visio::wire::v1::Header* header,
                        std::string* payload_out) {
  if (frame.size() < 3) {
    return FrameStatus::kFrameTooShort;
  }
  const std::uint8_t header_len = static_cast<std::uint8_t>(frame[0]);
  const std::size_t payload_end = 1u + header_len;
  if (payload_end + 2 > frame.size()) {
    return FrameStatus::kHeaderLenOverflow;
  }
  const std::string_view covered = frame.substr(0, frame.size() - 2);
  const std::uint16_t got_crc = ReadU16LE(frame.data() + frame.size() - 2);
  const std::uint16_t want_crc = Crc16(covered.data(), covered.size());
  if (got_crc != want_crc) {
    return FrameStatus::kCrcMismatch;
  }
  if (!header->ParseFromArray(frame.data() + 1, header_len)) {
    return FrameStatus::kHeaderParseError;
  }
  const std::size_t payload_len = frame.size() - 1 - 2 - header_len;
  payload_out->assign(frame.data() + payload_end, payload_len);
  return FrameStatus::kOk;
}

}  // namespace visio::wire
