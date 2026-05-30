#include "visio/wire/codec/frame.hpp"

#include <gtest/gtest.h>

#include <string>

#include "visio/wire/message.hpp"
#include "visio/wire/v1/header.pb.h"

namespace {

visio::wire::v1::Header MakeHeader() {
  visio::wire::v1::Header h;
  h.set_device(visio::wire::v1::DEVICE_GRIPPER_LEFT);
  h.set_routed_from(visio::wire::v1::DEVICE_GRIPPER_LEFT);
  h.set_stream(visio::wire::v1::STREAM_IMU_RAW);
  h.set_stream_index(3);
  h.set_seq(42);
  h.mutable_timestamp()->set_seconds(1700000000);
  h.mutable_timestamp()->set_nanos(123456789);
  return h;
}

TEST(Frame, RoundtripSimple) {
  const visio::wire::v1::Header header = MakeHeader();
  const std::string payload = "\xde\xad\xbe\xef";
  const std::string frame = visio::wire::EncodeFrame(header, payload);
  visio::wire::v1::Header decoded_header;
  std::string decoded_payload;
  const auto status =
      visio::wire::DecodeFrame(frame, &decoded_header, &decoded_payload);
  EXPECT_EQ(status, visio::wire::FrameStatus::kOk);
  EXPECT_EQ(decoded_header.SerializeAsString(), header.SerializeAsString());
  EXPECT_EQ(decoded_payload, payload);
}

TEST(Frame, RoundtripEmptyPayload) {
  const visio::wire::v1::Header header = MakeHeader();
  const std::string frame = visio::wire::EncodeFrame(header, "");
  visio::wire::v1::Header decoded_header;
  std::string decoded_payload;
  const auto status =
      visio::wire::DecodeFrame(frame, &decoded_header, &decoded_payload);
  EXPECT_EQ(status, visio::wire::FrameStatus::kOk);
  EXPECT_EQ(decoded_payload, "");
}

TEST(Frame, MessageRoundtrip) {
  visio::wire::Message msg;
  msg.device = visio::wire::v1::DEVICE_GLOVE_LEFT;
  msg.routed_from = visio::wire::v1::DEVICE_HOST;
  msg.stream = visio::wire::v1::STREAM_IMU_RAW;
  msg.stream_index = 7;
  msg.seq = 11;
  msg.payload = "\x01\x02\x03";

  const std::string frame =
      visio::wire::EncodeFrame(visio::wire::MsgToHeader(msg), msg.payload);
  visio::wire::v1::Header h;
  std::string payload;
  ASSERT_EQ(visio::wire::DecodeFrame(frame, &h, &payload),
            visio::wire::FrameStatus::kOk);
  const visio::wire::Message back = visio::wire::HeaderToMsg(h, payload);
  EXPECT_EQ(back.device, msg.device);
  EXPECT_EQ(back.stream, msg.stream);
  EXPECT_EQ(back.stream_index, msg.stream_index);
  EXPECT_EQ(back.seq, msg.seq);
  EXPECT_EQ(back.payload, msg.payload);
}

TEST(Frame, CrcMismatchDetected) {
  std::string frame = visio::wire::EncodeFrame(MakeHeader(), "hello");
  frame.back() ^= 0xFF;  // corrupt the last CRC byte
  visio::wire::v1::Header decoded_header;
  std::string decoded_payload;
  EXPECT_EQ(visio::wire::DecodeFrame(frame, &decoded_header, &decoded_payload),
            visio::wire::FrameStatus::kCrcMismatch);
}

TEST(Frame, HeaderLenOverflowDetected) {
  // First byte claims a 255-byte header, but the buffer is tiny.
  std::string bad;
  bad.push_back(static_cast<char>(0xFF));  // HEADER_LEN = 255
  bad.append("short");
  visio::wire::v1::Header decoded_header;
  std::string decoded_payload;
  EXPECT_EQ(visio::wire::DecodeFrame(bad, &decoded_header, &decoded_payload),
            visio::wire::FrameStatus::kHeaderLenOverflow);
}

TEST(Frame, ShortFrameDetected) {
  visio::wire::v1::Header decoded_header;
  std::string decoded_payload;
  EXPECT_EQ(
      visio::wire::DecodeFrame("\x00\x00", &decoded_header, &decoded_payload),
      visio::wire::FrameStatus::kFrameTooShort);
}

}  // namespace
