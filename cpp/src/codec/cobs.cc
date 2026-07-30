#include "visio_schema/wire/codec/cobs.hpp"

namespace visio_schema::wire {

std::vector<std::uint8_t> CobsEncode(std::string_view data) {
  // Sized up front to the worst case (one code byte per 254-byte block plus
  // the leading code byte) and filled through raw pointers: a per-byte
  // push_back pays a capacity check and branch on every input byte, which is
  // real CPU at the multi-MB/s rates the device frames at. The value-init
  // memset this constructor pays is the accepted cost — one vectorized pass,
  // far cheaper than per-byte growth checks.
  std::vector<std::uint8_t> out(data.size() + data.size() / 254 + 2);
  std::uint8_t* const base = out.data();
  std::uint8_t* p = base;
  std::uint8_t* code_ptr = p++;  // placeholder for first code byte
  std::uint8_t code = 1;
  // Whether the block at `code_ptr` still needs to be finalized. A full 0xFF
  // block opens a fresh block speculatively; if the input ends right there the
  // block stays empty and must be dropped, not emitted as a phantom 0x01. This
  // keeps the encoding canonical (byte-identical to the reference COBS used by
  // the Python side) — see docs/protocol/framing.md §3.2.
  bool block_open = true;
  for (char ch : data) {
    auto b = static_cast<std::uint8_t>(ch);
    if (b == 0) {
      *code_ptr = code;
      code_ptr = p++;
      code = 1;
      block_open = true;
    } else {
      *p++ = b;
      ++code;
      block_open = true;
      if (code == 0xFF) {
        *code_ptr = code;
        code_ptr = p++;
        code = 1;
        block_open = false;
      }
    }
  }
  if (block_open) {
    *code_ptr = code;
  } else {
    --p;  // drop the speculative empty block left by a trailing 0xFF flush
  }
  out.resize(static_cast<std::size_t>(p - base));
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
