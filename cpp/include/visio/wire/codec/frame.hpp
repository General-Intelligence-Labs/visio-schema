// Core wire frame: HEADER_LEN(u8) | header_pb | payload | CRC16(u16_le).
// Per visio-schema/docs/framing.md §1.
#pragma once

#include <cstdint>
#include <string>
#include <string_view>

#include "visio/wire/v1/header.pb.h"

namespace visio::wire {

enum class FrameStatus : std::uint8_t {
  kOk = 0,
  kFrameTooShort,
  kHeaderLenOverflow,
  kCrcMismatch,
  kHeaderParseError,
};

// Human-readable name for a FrameStatus (for log/drop messages).
const char* FrameStatusName(FrameStatus s) noexcept;

// Serialize header + payload into the core wire frame. Throws
// std::length_error if the serialized Header exceeds the u8 HEADER_LEN cap
// (255 bytes) — which the ~21-25 byte Header never approaches in practice.
std::string EncodeFrame(const visio::wire::v1::Header& header,
                        std::string_view payload);

// Parse a core wire frame. On success returns kOk and fills `header` /
// `payload_out`. On failure returns a typed status; callers log and drop.
FrameStatus DecodeFrame(std::string_view frame,
                        visio::wire::v1::Header* header,
                        std::string* payload_out);

}  // namespace visio::wire
