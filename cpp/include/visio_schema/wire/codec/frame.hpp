// Core wire frame: HEADER_LEN(u8) | header_pb | payload | CRC16(u16_le).
// Per visio-schema/docs/protocol/framing.md §1.
#pragma once

#include <cstdint>
#include <string>
#include <string_view>

#include "visio_schema/wire/message.hpp"

namespace visio_schema::wire {

enum class FrameStatus : std::uint8_t {
  kOk = 0,
  kFrameTooShort,
  kHeaderLenOverflow,
  kCrcMismatch,
  kHeaderParseError,
};

// Human-readable name for a FrameStatus (for log/drop messages).
const char* FrameStatusName(FrameStatus s) noexcept;

// Serialize a message's Header + payload into the core wire frame. The Header
// is a fixed, small protobuf (<= visio_schema_v1_wire_Header_size bytes, far
// under the u8 HEADER_LEN cap), so encoding is infallible.
std::string EncodeFrame(const Message& msg);

// Parse a core wire frame into `out`. On success returns kOk; on any
// shape/CRC/parse error returns a typed status and leaves `out` unspecified.
// Callers log and drop (framing.md §5).
FrameStatus DecodeFrame(std::string_view frame, Message* out);

}  // namespace visio_schema::wire
