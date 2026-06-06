// Cross-language golden wire vectors (C++ side).
//
// Loads the committed byte fixtures in tests/golden/wire_vectors.txt (path passed
// as VISIO_GOLDEN_DIR by CMake) and asserts the nanopb codec encodes the mirrored
// inputs to exactly those bytes and decodes them back. The Python test
// (python/tests/test_golden_vectors.py) pins the SAME bytes with mirrored inputs,
// so green on both sides proves nanopb (C++) and libprotobuf (Python) wire output
// are byte-identical.
#include <gtest/gtest.h>
#include <pb_decode.h>
#include <pb_encode.h>

#include <cstdint>
#include <fstream>
#include <map>
#include <string>
#include <vector>

#include "visio_schema/routing/registry.hpp"
#include "visio_schema/wire/codec/frame.hpp"
#include "visio_schema/wire/message.hpp"
#include "visio_schema/v1/wire/header.pb.h"

#ifndef VISIO_GOLDEN_DIR
#error "VISIO_GOLDEN_DIR must be defined by the build (path to tests/golden)"
#endif

namespace {

using visio_schema::Channel;
using visio_schema::routing::ChannelRegistry;
using visio_schema::wire::DecodeFrame;
using visio_schema::wire::EncodeFrame;
using visio_schema::wire::FrameStatus;
using visio_schema::wire::Message;

// Mirrored inputs — MUST match python/tests/test_golden_vectors.py.
constexpr std::uint32_t kStreamId = 16, kSeq = 7;
constexpr std::int64_t kTsS = 1234;
constexpr std::int32_t kTsN = 5678;
const std::string kPayload = "golden-payload";
const std::string kDevice = "gripper_left", kFirmware = "1.2.3";
const std::string kChTopic = "/gripper_left/imus/2/raw";
const std::string kChSchema = "visio_schema.v1.sensor.ImuRaw";

std::string FromHex(const std::string& h) {
  std::string out;
  out.reserve(h.size() / 2);
  for (std::size_t i = 0; i + 1 < h.size(); i += 2)
    out.push_back(static_cast<char>(std::stoi(h.substr(i, 2), nullptr, 16)));
  return out;
}

std::string Hex(const std::string& s) {
  static const char* d = "0123456789abcdef";
  std::string out;
  for (unsigned char c : s) {
    out.push_back(d[c >> 4]);
    out.push_back(d[c & 0xF]);
  }
  return out;
}

std::map<std::string, std::string> LoadGolden() {
  std::ifstream f(std::string(VISIO_GOLDEN_DIR) + "/wire_vectors.txt");
  EXPECT_TRUE(f.is_open()) << "cannot open golden fixture under " << VISIO_GOLDEN_DIR;
  std::map<std::string, std::string> out;
  std::string line;
  while (std::getline(f, line)) {
    if (line.empty() || line[0] == '#') continue;
    auto eq = line.find('=');
    if (eq == std::string::npos) continue;
    out[line.substr(0, eq)] = FromHex(line.substr(eq + 1));
  }
  return out;
}

TEST(GoldenVectors, Header) {
  auto vec = LoadGolden();
  visio_schema_v1_wire_Header h = visio_schema_v1_wire_Header_init_zero;
  h.stream_id = kStreamId;
  h.seq = kSeq;
  h.has_timestamp = true;
  h.timestamp.seconds = kTsS;
  h.timestamp.nanos = kTsN;
  std::uint8_t buf[64];
  pb_ostream_t os = pb_ostream_from_buffer(buf, sizeof(buf));
  ASSERT_TRUE(pb_encode(&os, visio_schema_v1_wire_Header_fields, &h));
  std::string got(reinterpret_cast<char*>(buf), os.bytes_written);
  EXPECT_EQ(Hex(got), Hex(vec["header"]));

  visio_schema_v1_wire_Header d = visio_schema_v1_wire_Header_init_zero;
  pb_istream_t is = pb_istream_from_buffer(
      reinterpret_cast<const pb_byte_t*>(vec["header"].data()), vec["header"].size());
  ASSERT_TRUE(pb_decode(&is, visio_schema_v1_wire_Header_fields, &d));
  EXPECT_EQ(d.stream_id, kStreamId);
  EXPECT_EQ(d.seq, kSeq);
  EXPECT_TRUE(d.has_timestamp);
  EXPECT_EQ(d.timestamp.seconds, kTsS);
  EXPECT_EQ(d.timestamp.nanos, kTsN);
}

TEST(GoldenVectors, Frame) {
  auto vec = LoadGolden();
  Message m;
  m.stream_id = kStreamId;
  m.seq = kSeq;
  m.payload = kPayload;
  m.timestamp.seconds = kTsS;
  m.timestamp.nanos = kTsN;
  EXPECT_EQ(Hex(EncodeFrame(m)), Hex(vec["frame"]));

  Message out;
  ASSERT_EQ(DecodeFrame(vec["frame"], &out), FrameStatus::kOk);
  EXPECT_EQ(out.stream_id, kStreamId);
  EXPECT_EQ(out.seq, kSeq);
  EXPECT_EQ(out.payload, kPayload);
  EXPECT_EQ(out.timestamp.seconds, kTsS);
  EXPECT_EQ(out.timestamp.nanos, kTsN);
}

TEST(GoldenVectors, DeviceInfo) {
  auto vec = LoadGolden();
  Channel c;
  c.id = 16;
  c.topic = kChTopic;
  c.schema_name = kChSchema;
  // encoding + schema_encoding default to "protobuf"; schema left empty.
  std::vector<Channel> chans{c};
  std::string di = ChannelRegistry::Encode(kDevice, kFirmware, "", "", 0, chans);
  EXPECT_EQ(Hex(di), Hex(vec["device_info"]));

  ChannelRegistry::DeviceView view;
  ASSERT_TRUE(ChannelRegistry::Decode(vec["device_info"], &view));
  EXPECT_EQ(view.device_name, kDevice);
  EXPECT_EQ(view.firmware_version, kFirmware);
  ASSERT_EQ(view.channels.size(), 1u);
  EXPECT_EQ(view.channels[0].id, 16u);
  EXPECT_EQ(view.channels[0].topic, kChTopic);
  EXPECT_EQ(view.channels[0].schema_name, kChSchema);
}

TEST(GoldenVectors, DeviceInfoWithInlineSchema) {
  auto vec = LoadGolden();
  Channel c;
  c.id = 16;
  c.topic = kChTopic;
  c.schema_name = kChSchema;
  c.schema = std::string("\x01\x02\x03", 3);  // non-empty bytes field
  std::vector<Channel> chans{c};
  std::string di = ChannelRegistry::Encode(kDevice, kFirmware, "", "", 0, chans);
  EXPECT_EQ(Hex(di), Hex(vec["device_info_with_schema"]));

  ChannelRegistry::DeviceView view;
  ASSERT_TRUE(ChannelRegistry::Decode(vec["device_info_with_schema"], &view));
  ASSERT_EQ(view.channels.size(), 1u);
  EXPECT_EQ(view.channels[0].schema, std::string("\x01\x02\x03", 3));
}

}  // namespace
