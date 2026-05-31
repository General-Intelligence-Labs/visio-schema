// CRC-16/CCITT-FALSE per visio-schema/docs/framing.md §4.
//
// Polynomial 0x1021, initial value 0xFFFF, no reflection, no XOR-out.
// Check value: Crc16("123456789", 9) == 0x29B1.
#pragma once

#include <cstddef>
#include <cstdint>

namespace visio_schema::wire {

// Compute CRC-16/CCITT-FALSE over `n` bytes at `data`.
//
// Byte-pointer (not std::span) so the wire codec stays C++17 — the embeddable
// target (RV1106 HDK vendor toolchain) is gcc 8.3 / -std=c++17, which has no
// <span>.
std::uint16_t Crc16(const void* data, std::size_t n) noexcept;

}  // namespace visio_schema::wire
