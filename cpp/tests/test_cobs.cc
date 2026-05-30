#include "visio/wire/codec/cobs.hpp"

#include <gtest/gtest.h>

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace {

std::string Decoded(const std::vector<std::uint8_t>& v) {
  return std::string(reinterpret_cast<const char*>(v.data()), v.size());
}

bool Roundtrip(std::string_view s, std::string* out) {
  std::vector<std::uint8_t> encoded = visio::wire::CobsEncode(s);
  for (std::uint8_t b : encoded) {
    if (b == 0) return false;  // no 0x00 may appear in the encoded run
  }
  std::vector<std::uint8_t> decoded;
  if (!visio::wire::CobsDecode(
          std::string_view(reinterpret_cast<const char*>(encoded.data()),
                           encoded.size()),
          &decoded)) {
    return false;
  }
  *out = Decoded(decoded);
  return true;
}

TEST(Cobs, RoundtripSimple) {
  for (std::string_view s :
       {std::string_view(""), std::string_view("A"),
        std::string_view("hello world"),
        std::string_view("\x00\x01\x02", 3)}) {
    std::string out;
    ASSERT_TRUE(Roundtrip(s, &out));
    EXPECT_EQ(out, std::string(s));
  }
}

TEST(Cobs, RoundtripAllByteValues) {
  std::string s;
  for (int i = 0; i < 256; ++i) s.push_back(static_cast<char>(i));
  std::string out;
  ASSERT_TRUE(Roundtrip(s, &out));
  EXPECT_EQ(out, s);
}

TEST(Cobs, RoundtripLongNonZeroRun) {
  const std::string s(300, '\xff');  // spans multiple 254-byte blocks
  std::string out;
  ASSERT_TRUE(Roundtrip(s, &out));
  EXPECT_EQ(out, s);
}

}  // namespace
