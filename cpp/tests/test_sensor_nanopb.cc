// Device-side (nanopb) round-trip for sensor payloads.
//
// CameraFrameInfo is all-scalar as of 0.7.0 (`frame_id` and `vi_time_ref` were
// removed), so it needs no nanopb.options bound and every field is FT_STATIC by
// construction — the whole message encodes into a fixed-size buffer with no
// callbacks. The round-trip below is what pins that: a field that ever degraded
// to a pb_callback_t would fail to encode here rather than silently drop on the
// wire.
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
  m.isp_frame_id = 14113;
  m.exposure_time_s = 0.00425676582f;
  m.analog_gain = 6.0f;
  m.digital_gain = 1.0f;
  m.isp_digital_gain = 1.5f;
  m.iso = 400;
  m.coarse_integration_time_lines = 313;
  m.line_length_pixels = 612;
  m.frame_length_lines = 2451;
  m.pixel_clock_mhz = 45.0f;

  std::string buf = Encode(visio_schema_v1_sensor_CameraFrameInfo_fields, m);
  Msg out = visio_schema_v1_sensor_CameraFrameInfo_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_sensor_CameraFrameInfo_fields, buf, &out));

  ASSERT_TRUE(out.has_timestamp);
  EXPECT_EQ(out.timestamp.seconds, 482);
  EXPECT_EQ(out.timestamp.nanos, 526755001);
  EXPECT_EQ(out.isp_frame_id, 14113u);
  EXPECT_FLOAT_EQ(out.exposure_time_s, 0.00425676582f);
  EXPECT_FLOAT_EQ(out.analog_gain, 6.0f);
  EXPECT_FLOAT_EQ(out.digital_gain, 1.0f);
  EXPECT_FLOAT_EQ(out.isp_digital_gain, 1.5f);
  EXPECT_EQ(out.iso, 400u);
  EXPECT_EQ(out.coarse_integration_time_lines, 313u);
  EXPECT_EQ(out.line_length_pixels, 612u);
  EXPECT_EQ(out.frame_length_lines, 2451u);
  EXPECT_FLOAT_EQ(out.pixel_clock_mhz, 45.0f);
}
