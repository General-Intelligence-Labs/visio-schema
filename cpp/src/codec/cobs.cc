#include "visio_schema/wire/codec/cobs.hpp"

namespace visio_schema::wire {

std::vector<std::uint8_t> CobsEncode(std::string_view data) {
  std::vector<std::uint8_t> out;
  out.reserve(data.size() + data.size() / 254 + 2);
  out.push_back(0);   // placeholder for first code byte
  std::size_t code_idx = 0;
  std::uint8_t code = 1;
  // Whether the block at `code_idx` still needs to be finalized. A full 0xFF
  // block opens a fresh block speculatively; if the input ends right there the
  // block stays empty and must be dropped, not emitted as a phantom 0x01. This
  // keeps the encoding canonical (byte-identical to the reference COBS used by
  // the Python side) — see docs/protocol/framing.md §3.2.
  bool block_open = true;
  for (char ch : data) {
    auto b = static_cast<std::uint8_t>(ch);
    if (b == 0) {
      out[code_idx] = code;
      out.push_back(0);
      code_idx = out.size() - 1;
      code = 1;
      block_open = true;
    } else {
      out.push_back(b);
      ++code;
      block_open = true;
      if (code == 0xFF) {
        out[code_idx] = code;
        out.push_back(0);
        code_idx = out.size() - 1;
        code = 1;
        block_open = false;
      }
    }
  }
  if (block_open) {
    out[code_idx] = code;
  } else {
    out.pop_back();  // drop the speculative empty block left by a trailing 0xFF flush
  }
  return out;
}

bool CobsDecode(std::string_view encoded, std::vector<std::uint8_t>* out) {
  std::size_t i = 0;
  while (i < encoded.size()) {
    auto code = static_cast<std::uint8_t>(encoded[i]);
    if (code == 0) return false;
    std::size_t end = i + code;
    if (end > encoded.size()) return false;
    for (std::size_t j = i + 1; j < end; ++j) {
      auto b = static_cast<std::uint8_t>(encoded[j]);
      if (b == 0) return false;
      out->push_back(b);
    }
    i = end;
    // Append an implicit zero unless this was the last block, or this
    // block was a full 254-byte non-zero run.
    if (i < encoded.size() && code != 0xFF) {
      out->push_back(0);
    }
  }
  return true;
}

}  // namespace visio_schema::wire
