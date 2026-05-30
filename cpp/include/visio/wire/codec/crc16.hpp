// CRC-16/CCITT-FALSE per visio-schema/docs/framing.md §4.
//
// Polynomial 0x1021, initial value 0xFFFF, no reflection, no XOR-out.
// Check value: Crc16("123456789", 9) == 0x29B1.
#pragma once

#include <cstddef>
#include <cstdint>
#include <span>

namespace visio::wire {

// Compute CRC-16/CCITT-FALSE over `data`.
std::uint16_t Crc16(std::span<const std::byte> data) noexcept;
std::uint16_t Crc16(const void* data, std::size_t n) noexcept;

}  // namespace visio::wire
