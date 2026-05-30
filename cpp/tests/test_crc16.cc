#include "visio_schema/wire/codec/crc16.hpp"

#include <gtest/gtest.h>

#include <string>
#include <string_view>

namespace {

std::uint16_t Crc(std::string_view s) {
  return visio_schema::wire::Crc16(s.data(), s.size());
}

TEST(Crc16, CheckValue) {
  EXPECT_EQ(Crc("123456789"), 0x29B1);
}

TEST(Crc16, Empty) {
  EXPECT_EQ(Crc(""), 0xFFFF);
}

TEST(Crc16, KnownValues) {
  EXPECT_EQ(Crc("A"), 0xB915);
  EXPECT_EQ(Crc("AB"), 0x4B74);
  EXPECT_EQ(Crc(std::string(16, '\xff')), 0x6A4B);
}

}  // namespace
