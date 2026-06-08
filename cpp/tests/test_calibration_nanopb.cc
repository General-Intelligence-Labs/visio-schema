// Device-side (nanopb) round-trip for the calibration messages. The
// nanopb.options max_count/max_size bounds make foxglove.CameraCalibration and
// foxglove.FrameTransform fully FT_STATIC, so a SetCalibration command encodes
// and decodes with no callbacks and no malloc — this test proves that.
#include <gtest/gtest.h>

#include <pb_decode.h>
#include <pb_encode.h>

#include <string>
#include <vector>

#include "foxglove/CameraCalibration.pb.h"
#include "foxglove/FrameTransform.pb.h"
#include "visio_schema/v1/calibration/imu.pb.h"
#include "visio_schema/v1/control/command.pb.h"

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

TEST(CalibrationNanopb, SetCalibrationCameraIntrinsicsRoundTrip) {
  visio_schema_v1_control_Command cmd = visio_schema_v1_control_Command_init_zero;
  std::snprintf(cmd.target_device, sizeof(cmd.target_device), "ego");
  cmd.command_id = 7;
  cmd.which_body = visio_schema_v1_control_Command_set_calibration_tag;
  auto& sc = cmd.body.set_calibration;
  sc.sensor_kind = visio_schema_v1_control_SetCalibration_SensorKind_CAMERA;
  sc.sensor_index = 0;
  sc.which_artifact = visio_schema_v1_control_SetCalibration_intrinsics_tag;
  auto& cc = sc.artifact.intrinsics;
  cc.width = 1920;
  cc.height = 1080;
  std::snprintf(cc.distortion_model, sizeof(cc.distortion_model), "kannala_brandt");
  cc.D_count = 4;
  cc.D[0] = 0.0035; cc.D[1] = 0.0007; cc.D[2] = -0.0021; cc.D[3] = 0.0002;
  cc.K_count = 9;
  for (int i = 0; i < 9; ++i) cc.K[i] = i + 1;

  std::string buf = Encode(visio_schema_v1_control_Command_fields, cmd);
  visio_schema_v1_control_Command out = visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields, buf, &out));

  EXPECT_STREQ(out.target_device, "ego");
  EXPECT_EQ(out.command_id, 7u);
  ASSERT_EQ(out.which_body, visio_schema_v1_control_Command_set_calibration_tag);
  const auto& osc = out.body.set_calibration;
  EXPECT_EQ(osc.sensor_kind, visio_schema_v1_control_SetCalibration_SensorKind_CAMERA);
  ASSERT_EQ(osc.which_artifact, visio_schema_v1_control_SetCalibration_intrinsics_tag);
  const auto& oc = osc.artifact.intrinsics;
  EXPECT_EQ(oc.width, 1920u);
  EXPECT_STREQ(oc.distortion_model, "kannala_brandt");
  ASSERT_EQ(oc.D_count, 4u);
  EXPECT_DOUBLE_EQ(oc.D[0], 0.0035);
  ASSERT_EQ(oc.K_count, 9u);
  EXPECT_DOUBLE_EQ(oc.K[8], 9.0);
}

TEST(CalibrationNanopb, SetCalibrationExtrinsicsRoundTrip) {
  visio_schema_v1_control_Command cmd = visio_schema_v1_control_Command_init_zero;
  cmd.which_body = visio_schema_v1_control_Command_set_calibration_tag;
  auto& sc = cmd.body.set_calibration;
  sc.sensor_kind = visio_schema_v1_control_SetCalibration_SensorKind_IMU;
  sc.which_artifact = visio_schema_v1_control_SetCalibration_extrinsics_tag;
  auto& ft = sc.artifact.extrinsics;
  std::snprintf(ft.parent_frame_id, sizeof(ft.parent_frame_id), "cam0");
  std::snprintf(ft.child_frame_id, sizeof(ft.child_frame_id), "imu0");
  ft.has_translation = true;
  ft.translation.x = 0.01; ft.translation.z = -0.05;
  ft.has_rotation = true;
  ft.rotation.w = 1.0;

  std::string buf = Encode(visio_schema_v1_control_Command_fields, cmd);
  visio_schema_v1_control_Command out = visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields, buf, &out));

  const auto& oft = out.body.set_calibration.artifact.extrinsics;
  EXPECT_STREQ(oft.parent_frame_id, "cam0");
  EXPECT_STREQ(oft.child_frame_id, "imu0");
  EXPECT_DOUBLE_EQ(oft.translation.x, 0.01);
  EXPECT_DOUBLE_EQ(oft.rotation.w, 1.0);
}

TEST(CalibrationNanopb, ImuCalibrationNoiseRoundTrip) {
  visio_schema_v1_calibration_ImuCalibration ic =
      visio_schema_v1_calibration_ImuCalibration_init_zero;
  ic.accel_noise_density = 0.008;
  ic.gyro_random_walk = 2.0e-5;
  ic.update_rate_hz = 200.0;
  ic.time_offset_to_cam0_s = -0.0123;
  ic.accel_misalignment_count = 9;
  for (int i = 0; i < 9; ++i) ic.accel_misalignment[i] = (i % 4 == 0) ? 1.0 : 0.0;

  std::string buf = Encode(visio_schema_v1_calibration_ImuCalibration_fields, ic);
  visio_schema_v1_calibration_ImuCalibration out =
      visio_schema_v1_calibration_ImuCalibration_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_calibration_ImuCalibration_fields, buf, &out));

  EXPECT_DOUBLE_EQ(out.accel_noise_density, 0.008);
  EXPECT_DOUBLE_EQ(out.gyro_random_walk, 2.0e-5);
  EXPECT_DOUBLE_EQ(out.time_offset_to_cam0_s, -0.0123);
  ASSERT_EQ(out.accel_misalignment_count, 9u);
  EXPECT_DOUBLE_EQ(out.accel_misalignment[0], 1.0);
}

// A Command targeted at another device is still decodable here (filtering is the
// receiver's job); this asserts target_device is a readable static field now.
TEST(CalibrationNanopb, TargetDeviceIsReadable) {
  visio_schema_v1_control_Command cmd = visio_schema_v1_control_Command_init_zero;
  std::snprintf(cmd.target_device, sizeof(cmd.target_device), "gripper_left");
  std::string buf = Encode(visio_schema_v1_control_Command_fields, cmd);
  visio_schema_v1_control_Command out = visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields, buf, &out));
  EXPECT_STREQ(out.target_device, "gripper_left");
}
