#include "visio/wire/codec/crc16.hpp"

#include <array>

namespace visio::wire {

namespace {

constexpr std::array<std::uint16_t, 256> MakeTable() {
  std::array<std::uint16_t, 256> table{};
  for (int i = 0; i < 256; ++i) {
    std::uint16_t crc = static_cast<std::uint16_t>(i) << 8;
    for (int j = 0; j < 8; ++j) {
      crc = (crc & 0x8000) ? static_cast<std::uint16_t>((crc << 1) ^ 0x1021)
                           : static_cast<std::uint16_t>(crc << 1);
    }
    table[i] = crc;
  }
  return table;
}

constexpr auto kTable = MakeTable();

}  // namespace

std::uint16_t Crc16(std::span<const std::byte> data) noexcept {
  std::uint16_t crc = 0xFFFF;
  for (std::byte b : data) {
    crc = static_cast<std::uint16_t>(
        (crc << 8) ^ kTable[((crc >> 8) ^ std::to_integer<std::uint8_t>(b)) & 0xFF]);
  }
  return crc;
}

std::uint16_t Crc16(const void* data, std::size_t n) noexcept {
  return Crc16({static_cast<const std::byte*>(data), n});
}

}  // namespace visio::wire
