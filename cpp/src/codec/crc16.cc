#include "visio_schema/wire/codec/crc16.hpp"

#include <array>

namespace visio_schema::wire {

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

std::uint16_t Crc16(const void* data, std::size_t n) noexcept {
  const auto* bytes = static_cast<const std::uint8_t*>(data);
  std::uint16_t crc = 0xFFFF;
  for (std::size_t i = 0; i < n; ++i) {
    crc = static_cast<std::uint16_t>(
        (crc << 8) ^ kTable[((crc >> 8) ^ bytes[i]) & 0xFF]);
  }
  return crc;
}

}  // namespace visio_schema::wire
