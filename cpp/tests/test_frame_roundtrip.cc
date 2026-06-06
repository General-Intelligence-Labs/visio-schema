#include "visio_schema/wire/codec/frame.hpp"

#include <gtest/gtest.h>

#include "visio_schema/wire/message.hpp"

using visio_schema::wire::DecodeFrame;
using visio_schema::wire::EncodeFrame;
using visio_schema::wire::FrameStatus;
using visio_schema::wire::Message;

namespace {

Message MakeMsg() {
  Message m;
  // A dynamic data stream id (>= CONTROL_STREAM_FIRST_DYNAMIC).
  m.stream_id = visio_schema_v1_wire_ControlStream_CONTROL_STREAM_FIRST_DYNAMIC + 3;
  m.seq = 42;
  m.timestamp.seconds = 1'700'000'000;
  m.timestamp.nanos = 123'456'789;
  return m;
}

}  // namespace

TEST(FrameTest, RoundtripSimple) {
  Message m = MakeMsg();
  m.payload = "\xde\xad\xbe\xef";
  Message decoded;
  ASSERT_EQ(DecodeFrame(EncodeFrame(m), &decoded), FrameStatus::kOk);
  EXPECT_EQ(decoded.stream_id, m.stream_id);
  EXPECT_EQ(decoded.seq, m.seq);
  EXPECT_EQ(decoded.timestamp.seconds, m.timestamp.seconds);
  EXPECT_EQ(decoded.timestamp.nanos, m.timestamp.nanos);
  EXPECT_EQ(decoded.payload, m.payload);
}

TEST(FrameTest, RoundtripEmptyPayload) {
  Message decoded;
  ASSERT_EQ(DecodeFrame(EncodeFrame(MakeMsg()), &decoded), FrameStatus::kOk);
  EXPECT_TRUE(decoded.payload.empty());
}

// A relay must forward a stream id it doesn't know — the hub decodes the
// Header and re-emits the opaque payload verbatim. This is the guarantee that
// survives dropping descriptor reflection on the embedded side.
TEST(FrameTest, UnknownStreamRelays) {
  Message m = MakeMsg();
  m.stream_id = 9999;  // an id this peer has no mapping for
  m.payload = std::string("\x01\x02\x03", 3);
  Message decoded;
  ASSERT_EQ(DecodeFrame(EncodeFrame(m), &decoded), FrameStatus::kOk);
  EXPECT_EQ(decoded.stream_id, 9999u);
  EXPECT_EQ(decoded.payload, std::string("\x01\x02\x03", 3));
}

TEST(FrameTest, CorruptCrcRejected) {
  Message m = MakeMsg();
  m.payload = "hello";
  std::string frame = EncodeFrame(m);
  frame.back() ^= 0xFF;
  Message decoded;
  EXPECT_EQ(DecodeFrame(frame, &decoded), FrameStatus::kCrcMismatch);
}

TEST(FrameTest, FrameTooShortRejected) {
  Message decoded;
  EXPECT_EQ(DecodeFrame(std::string("\x01", 1), &decoded),
            FrameStatus::kFrameTooShort);
}

TEST(FrameTest, HeaderLenOverflowRejected) {
  std::string bad;
  bad.push_back(static_cast<char>(0xFF));  // HEADER_LEN = 255, buffer far shorter
  bad.append("short");
  Message decoded;
  EXPECT_EQ(DecodeFrame(bad, &decoded), FrameStatus::kHeaderLenOverflow);
}
