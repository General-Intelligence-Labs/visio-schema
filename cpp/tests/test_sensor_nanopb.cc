// Device-side (nanopb) round-trip for sensor payloads.
//
// CameraFrameInfo is all-scalar, so it needs no nanopb.options bound and every
// field is FT_STATIC by construction — the whole message encodes into a
// fixed-size buffer with no callbacks. The round-trip below is what pins that: a
// field that ever degraded to a pb_callback_t would fail to encode here rather
// than silently drop on the wire. It also pins the signed midpoint offset (a
// zigzag sint32, usually negative) and the nested readout-direction enum.
#include <gtest/gtest.h>

#include <pb_decode.h>
#include <pb_encode.h>

#include <string>

#include "visio_schema/v1/sensor/camera_frame_info.pb.h"

namespace {

template <typename T>
std::string Encode(const pb_msgdesc_t* fields, const T& msg) {
  std::size_t sz = 0;
  EXPECT_TRUE(pb_get_encoded_size(&sz, fields, &msg));
  std::string out(sz, '\0');
  pb_ostream_t os = pb_ostream_from_buffer(reinterpret_cast<pb_byte_t*>(&out[0]), sz);
  EXPECT_TRUE(pb_encode(&os, fields, &msg));
  out.resize(os.bytes_written);
  return out;
}

template <typename T>
bool Decode(const pb_msgdesc_t* fields, const std::string& buf, T* out) {
  pb_istream_t is = pb_istream_from_buffer(
      reinterpret_cast<const pb_byte_t*>(buf.data()), buf.size());
  return pb_decode(&is, fields, out);
}

}  // namespace

TEST(SensorNanopb, CameraFrameInfoRoundTrip) {
  using Msg = visio_schema_v1_sensor_CameraFrameInfo;

  Msg m = visio_schema_v1_sensor_CameraFrameInfo_init_zero;
  m.has_timestamp = true;
  // Non-round ns split across the Timestamp fields.
  m.timestamp.seconds = 482;
  m.timestamp.nanos = 526755001;
  m.exposure_us = 29832;
  m.exposure_mid_offset_us = -15147;
  m.gain = 6.5f;
  m.line_delay_ns = 24510;
  m.readout_direction =
      visio_schema_v1_sensor_CameraFrameInfo_ReadoutDirection_READOUT_DIRECTION_BOTTOM_TO_TOP;

  std::string buf = Encode(visio_schema_v1_sensor_CameraFrameInfo_fields, m);
  Msg out = visio_schema_v1_sensor_CameraFrameInfo_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_sensor_CameraFrameInfo_fields, buf, &out));

  ASSERT_TRUE(out.has_timestamp);
  EXPECT_EQ(out.timestamp.seconds, 482);
  EXPECT_EQ(out.timestamp.nanos, 526755001);
  EXPECT_EQ(out.exposure_us, 29832u);
  EXPECT_EQ(out.exposure_mid_offset_us, -15147);
  EXPECT_FLOAT_EQ(out.gain, 6.5f);
  EXPECT_EQ(out.line_delay_ns, 24510u);
  EXPECT_EQ(out.readout_direction,
            visio_schema_v1_sensor_CameraFrameInfo_ReadoutDirection_READOUT_DIRECTION_BOTTOM_TO_TOP);
}
