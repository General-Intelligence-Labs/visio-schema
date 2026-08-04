#include "visio_schema/wire/codec/cobs.hpp"

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
  std::vector<std::uint8_t> encoded = visio_schema::wire::CobsEncode(s);
  for (std::uint8_t b : encoded) {
    if (b == 0) return false;  // no 0x00 may appear in the encoded run
  }
  std::vector<std::uint8_t> decoded;
  if (!visio_schema::wire::CobsDecode(
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

// Exact-byte golden vectors pinning the canonical COBS encoding. These must
// stay byte-identical to the Python side (python/tests/test_cobs.py) — the
// 254-byte-multiple cases are where a non-canonical encoder emits a phantom
// trailing 0x01 block. See docs/protocol/framing.md §3.2.
TEST(Cobs, GoldenVectors) {
  struct Case {
    std::string_view input;
    std::vector<std::uint8_t> expected;
  };
  const std::string ff254(254, '\xff');
  const std::string ff255(255, '\xff');
  const std::string ff508(508, '\xff');

  // empty -> single empty block.
  EXPECT_EQ(visio_schema::wire::CobsEncode(""),
            (std::vector<std::uint8_t>{0x01}));

  // 254 non-zero bytes end exactly on a 0xFF block: 0xFF code + 254 data, and
  // crucially NO trailing block. Result is 255 bytes, all 0xFF.
  EXPECT_EQ(visio_schema::wire::CobsEncode(ff254),
            std::vector<std::uint8_t>(255, 0xFF));

  // One past the boundary: full 0xFF block, then a 2-byte block (0x02, 0xFF).
  std::vector<std::uint8_t> exp255(255, 0xFF);
  exp255.push_back(0x02);
  exp255.push_back(0xFF);
  EXPECT_EQ(visio_schema::wire::CobsEncode(ff255), exp255);

  // Two full 0xFF blocks back to back, again no trailing phantom block.
  EXPECT_EQ(visio_schema::wire::CobsEncode(ff508),
            std::vector<std::uint8_t>(510, 0xFF));
}

// The pre-sized pointer-fill encoder is tightest exactly at the all-zero and
// block-boundary cases — pin them so an off-by-one in the worst-case bound
// fails a test instead of writing past the buffer.
TEST(Cobs, GoldenVectorsZerosAndBlockBoundaries) {
  using visio_schema::wire::CobsEncode;
  // All-zero input: n zeros -> n+1 code bytes of 0x01 (max code-byte ratio,
  // the tightest fit against the worst-case allocation).
  EXPECT_EQ(CobsEncode(std::string_view("\0", 1)),
            (std::vector<std::uint8_t>{0x01, 0x01}));
  EXPECT_EQ(CobsEncode(std::string(32, '\0')),
            std::vector<std::uint8_t>(33, 0x01));
  // One under the block boundary: 0xFE code + 253 data, closed by the input
  // ending rather than the 0xFF flush.
  std::vector<std::uint8_t> exp253{0xFE};
  exp253.insert(exp253.end(), 253, 0xFF);
  EXPECT_EQ(CobsEncode(std::string(253, '\xff')), exp253);
  // A zero right after a full block: the flush's speculative block closes as
  // 0x01, then the zero opens the final empty block.
  std::vector<std::uint8_t> exp254z(255, 0xFF);
  exp254z.push_back(0x01);
  exp254z.push_back(0x01);
  EXPECT_EQ(CobsEncode(std::string(254, '\xff') + '\0'), exp254z);
}

}  // namespace
