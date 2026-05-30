#include "visio_schema/wire/codec/frame.hpp"

#include <gtest/gtest.h>

#include <string>

#include "visio_schema/wire/message.hpp"
#include "visio_schema/wire/v1/header.pb.h"

namespace {

visio_schema::wire::v1::Header MakeHeader() {
  visio_schema::wire::v1::Header h;
  h.set_device(visio_schema::wire::v1::DEVICE_GRIPPER_LEFT);
  h.set_routed_from(visio_schema::wire::v1::DEVICE_GRIPPER_LEFT);
  h.set_stream(visio_schema::wire::v1::STREAM_IMU_RAW);
  h.set_stream_index(3);
  h.set_seq(42);
  h.mutable_timestamp()->set_seconds(1700000000);
  h.mutable_timestamp()->set_nanos(123456789);
  return h;
}

TEST(Frame, RoundtripSimple) {
  const visio_schema::wire::v1::Header header = MakeHeader();
  const std::string payload = "\xde\xad\xbe\xef";
  const std::string frame = visio_schema::wire::EncodeFrame(header, payload);
  visio_schema::wire::v1::Header decoded_header;
  std::string decoded_payload;
  const auto status =
      visio_schema::wire::DecodeFrame(frame, &decoded_header, &decoded_payload);
  EXPECT_EQ(status, visio_schema::wire::FrameStatus::kOk);
  EXPECT_EQ(decoded_header.SerializeAsString(), header.SerializeAsString());
  EXPECT_EQ(decoded_payload, payload);
}

TEST(Frame, RoundtripEmptyPayload) {
  const visio_schema::wire::v1::Header header = MakeHeader();
  const std::string frame = visio_schema::wire::EncodeFrame(header, "");
  visio_schema::wire::v1::Header decoded_header;
  std::string decoded_payload;
  const auto status =
      visio_schema::wire::DecodeFrame(frame, &decoded_header, &decoded_payload);
  EXPECT_EQ(status, visio_schema::wire::FrameStatus::kOk);
  EXPECT_EQ(decoded_payload, "");
}

TEST(Frame, MessageRoundtrip) {
  visio_schema::wire::Message msg;
  msg.device = visio_schema::wire::v1::DEVICE_GLOVE_LEFT;
  msg.routed_from = visio_schema::wire::v1::DEVICE_HOST;
  msg.stream = visio_schema::wire::v1::STREAM_IMU_RAW;
  msg.stream_index = 7;
  msg.seq = 11;
  msg.payload = "\x01\x02\x03";

  const std::string frame =
      visio_schema::wire::EncodeFrame(visio_schema::wire::MsgToHeader(msg), msg.payload);
  visio_schema::wire::v1::Header h;
  std::string payload;
  ASSERT_EQ(visio_schema::wire::DecodeFrame(frame, &h, &payload),
            visio_schema::wire::FrameStatus::kOk);
  const visio_schema::wire::Message back = visio_schema::wire::HeaderToMsg(h, payload);
  EXPECT_EQ(back.device, msg.device);
  EXPECT_EQ(back.stream, msg.stream);
  EXPECT_EQ(back.stream_index, msg.stream_index);
  EXPECT_EQ(back.seq, msg.seq);
  EXPECT_EQ(back.payload, msg.payload);
}

TEST(Frame, CrcMismatchDetected) {
  std::string frame = visio_schema::wire::EncodeFrame(MakeHeader(), "hello");
  frame.back() ^= 0xFF;  // corrupt the last CRC byte
  visio_schema::wire::v1::Header decoded_header;
  std::string decoded_payload;
  EXPECT_EQ(visio_schema::wire::DecodeFrame(frame, &decoded_header, &decoded_payload),
            visio_schema::wire::FrameStatus::kCrcMismatch);
}

TEST(Frame, HeaderLenOverflowDetected) {
  // First byte claims a 255-byte header, but the buffer is tiny.
  std::string bad;
  bad.push_back(static_cast<char>(0xFF));  // HEADER_LEN = 255
  bad.append("short");
  visio_schema::wire::v1::Header decoded_header;
  std::string decoded_payload;
  EXPECT_EQ(visio_schema::wire::DecodeFrame(bad, &decoded_header, &decoded_payload),
            visio_schema::wire::FrameStatus::kHeaderLenOverflow);
}

TEST(Frame, ShortFrameDetected) {
  visio_schema::wire::v1::Header decoded_header;
  std::string decoded_payload;
  EXPECT_EQ(
      visio_schema::wire::DecodeFrame("\x00\x00", &decoded_header, &decoded_payload),
      visio_schema::wire::FrameStatus::kFrameTooShort);
}

}  // namespace
