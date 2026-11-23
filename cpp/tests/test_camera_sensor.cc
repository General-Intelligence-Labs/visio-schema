#include <gtest/gtest.h>
#include <pb_decode.h>
#include <pb_encode.h>

#include <string>

#include "visio_schema/v1/control/command_result.pb.h"

namespace {

using State = visio_schema_v1_control_DeviceState;
const auto* const kFields = visio_schema_v1_control_DeviceState_fields;

bool Decode(const std::string& wire, State* state) {
  auto input = pb_istream_from_buffer(
      reinterpret_cast<const pb_byte_t*>(wire.data()), wire.size());
  return pb_decode(&input, kFields, state);
}

}  // namespace

TEST(CameraSensorWire, PythonState) {
  // Python protobuf DeviceState with indices 0, 3, 5 and three sensor models.
  const std::string wire =
      std::string("\xda\x03\x08\x12\x06" "ar0234", 11) +
      std::string("\xda\x03\x0a\x08\x03\x12\x06" "ar0235", 13) +
      std::string("\xda\x03\x0c\x08\x05\x12\x08" "sc235hai", 15);
  State state = visio_schema_v1_control_DeviceState_init_zero;
  ASSERT_TRUE(Decode(wire, &state));
  ASSERT_EQ(state.camera_sensors_count, 3u);
  EXPECT_EQ(state.camera_sensors[0].camera_index, 0u);
  EXPECT_STREQ(state.camera_sensors[0].sensor_model, "ar0234");
  EXPECT_EQ(state.camera_sensors[1].camera_index, 3u);
  EXPECT_STREQ(state.camera_sensors[1].sensor_model, "ar0235");
  EXPECT_EQ(state.camera_sensors[2].camera_index, 5u);
  EXPECT_STREQ(state.camera_sensors[2].sensor_model, "sc235hai");
  EXPECT_EQ(sizeof(state.camera_sensors) / sizeof(state.camera_sensors[0]),
            8u);
  EXPECT_EQ(sizeof(state.camera_sensors[0].sensor_model), 32u);
  std::string encoded(wire.size(), '\0');
  auto output = pb_ostream_from_buffer(
      reinterpret_cast<pb_byte_t*>(encoded.data()), encoded.size());
  ASSERT_TRUE(pb_encode(&output, kFields, &state));
  encoded.resize(output.bytes_written);
  EXPECT_EQ(encoded, wire);
}

TEST(CameraSensorWire, CapacityBounds) {
  // Independent wire vectors can exceed the generated static arrays.
  const std::string model(31, 'm');
  std::string wire;
  for (unsigned int index = 0; index < 8; ++index) {
    wire += std::string("\xda\x03\x23\x08", 4);
    wire.push_back(static_cast<char>(index));
    wire += std::string("\x12\x1f", 2) + model;
  }
  State state = visio_schema_v1_control_DeviceState_init_zero;
  ASSERT_TRUE(Decode(wire, &state));
  ASSERT_EQ(state.camera_sensors_count, 8u);
  for (unsigned int index = 0; index < 8; ++index) {
    EXPECT_EQ(state.camera_sensors[index].camera_index, index);
    EXPECT_STREQ(state.camera_sensors[index].sensor_model, model.c_str());
  }
  std::string encoded(wire.size(), '\0');
  auto output = pb_ostream_from_buffer(
      reinterpret_cast<pb_byte_t*>(encoded.data()), encoded.size());
  ASSERT_TRUE(pb_encode(&output, kFields, &state));
  encoded.resize(output.bytes_written);
  State decoded = visio_schema_v1_control_DeviceState_init_zero;
  ASSERT_TRUE(Decode(encoded, &decoded));
  ASSERT_EQ(decoded.camera_sensors_count, 8u);
  for (unsigned int index = 0; index < 8; ++index) {
    EXPECT_EQ(decoded.camera_sensors[index].camera_index, index);
    EXPECT_STREQ(decoded.camera_sensors[index].sensor_model, model.c_str());
  }
  wire += std::string("\xda\x03\x04\x08\x08\x12\x00", 7);
  State over_count = visio_schema_v1_control_DeviceState_init_zero;
  EXPECT_FALSE(Decode(wire, &over_count));
  const std::string oversized =
      std::string("\xda\x03\x22\x12\x20", 5) + std::string(32, 'm');
  State over_model = visio_schema_v1_control_DeviceState_init_zero;
  EXPECT_FALSE(Decode(oversized, &over_model));
}
