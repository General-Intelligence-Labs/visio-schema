// COBS encode/decode per visio-schema/docs/framing.md §3.2.
//
// On the wire a serial frame is `Encode(payload) || 0x00`. The trailing 0x00
// is the inter-frame delimiter; Encode guarantees no other 0x00 bytes appear
// in the encoded run. The functions in this header operate on the
// inside-of-delimiter run only.
#pragma once

#include <cstdint>
#include <string_view>
#include <vector>

namespace visio_schema::wire {

// Return the COBS encoding of `data` (no trailing 0x00 delimiter).
std::vector<std::uint8_t> CobsEncode(std::string_view data);

// COBS-decode `encoded`. Returns false on malformed input (zero byte
// before the end, zero-length input). `out` is appended to on success.
bool CobsDecode(std::string_view encoded, std::vector<std::uint8_t>* out);

}  // namespace visio_schema::wire
