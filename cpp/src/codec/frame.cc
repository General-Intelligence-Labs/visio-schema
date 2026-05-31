#include "visio_schema/wire/codec/frame.hpp"

#include <pb_decode.h>
#include <pb_encode.h>

#include "visio_schema/wire/codec/crc16.hpp"

namespace visio_schema::wire {

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

visio_schema_wire_v1_Header ToHeader(const Message& msg) {
  visio_schema_wire_v1_Header h = visio_schema_wire_v1_Header_init_zero;
  h.device = msg.device;
  h.routed_from = msg.routed_from;
  h.stream = msg.stream;
  h.stream_index = msg.stream_index;
  h.seq = msg.seq;
  h.has_timestamp = true;
  h.timestamp = msg.timestamp;
  return h;
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

std::string EncodeFrame(const Message& msg) {
  visio_schema_wire_v1_Header header = ToHeader(msg);
  // The Header is bounded by the generated max size, which is far under the u8
  // HEADER_LEN cap — so this stack buffer always fits and pb_encode can't fail.
  pb_byte_t hbuf[visio_schema_wire_v1_Header_size];
  pb_ostream_t os = pb_ostream_from_buffer(hbuf, sizeof(hbuf));
  pb_encode(&os, visio_schema_wire_v1_Header_fields, &header);
  const std::size_t header_len = os.bytes_written;

  std::string out;
  out.reserve(1 + header_len + msg.payload.size() + 2);
  out.push_back(static_cast<char>(header_len));  // HEADER_LEN (u8)
  out.append(reinterpret_cast<const char*>(hbuf), header_len);
  out.append(msg.payload);
  const std::uint16_t crc = Crc16(out.data(), out.size());
  PackU16LE(&out, crc);
  return out;
}

FrameStatus DecodeFrame(std::string_view frame, Message* out) {
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
  if (got_crc != Crc16(covered.data(), covered.size())) {
    return FrameStatus::kCrcMismatch;
  }

  visio_schema_wire_v1_Header header = visio_schema_wire_v1_Header_init_zero;
  pb_istream_t is = pb_istream_from_buffer(
      reinterpret_cast<const pb_byte_t*>(frame.data() + 1), header_len);
  if (!pb_decode(&is, visio_schema_wire_v1_Header_fields, &header)) {
    return FrameStatus::kHeaderParseError;
  }

  out->device = header.device;
  out->routed_from = header.routed_from;
  out->stream = header.stream;
  out->stream_index = header.stream_index;
  out->seq = header.seq;
  out->timestamp = header.timestamp;
  out->payload.assign(frame.data() + payload_end,
                      frame.size() - payload_end - 2);
  return FrameStatus::kOk;
}

}  // namespace visio_schema::wire
