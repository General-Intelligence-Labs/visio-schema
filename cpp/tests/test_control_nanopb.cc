// Device-side (nanopb) round-trip for the sealed provisioning envelope.
//
// The `sealed` fields are `bytes`, and a bytes field with no max_size in
// nanopb.options becomes a pb_callback_t — which the firmware's static decode
// path cannot use, so the field would silently never arrive and a v2 settings
// QR would appear to apply while setting nothing. Everything asserted here is
// what makes the cap real on the device rather than a number in a text file.
#include <gtest/gtest.h>

#include <pb_decode.h>
#include <pb_encode.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <type_traits>

#include "visio_schema/v1/control/command.pb.h"
#include "visio_schema/v1/control/command_result.pb.h"

namespace {

template <typename T>
std::string Encode(const pb_msgdesc_t* fields, const T& msg) {
  std::size_t sz = 0;
  EXPECT_TRUE(pb_get_encoded_size(&sz, fields, &msg));
  std::string out(sz, '\0');
  pb_ostream_t os =
      pb_ostream_from_buffer(reinterpret_cast<pb_byte_t*>(&out[0]), sz);
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

// The cap in proto/nanopb.options, mirrored by
// visio_schema.settings_qr.payload.SEALED_MAX_BYTES (pinned equal by
// python/tests/test_nanopb_options.py).
constexpr std::size_t kSealedMax = 384;

// A blob shaped like a real envelope: "VSL1" || kid || ephemeral pubkey || ct.
std::string MakeEnvelope(std::size_t n) {
  std::string blob = "VSL1";
  blob += std::string("\xee\xdd\x88\x3d", 4);
  for (std::size_t i = blob.size(); i < n; ++i) {
    blob.push_back(static_cast<char>(i & 0xFF));
  }
  return blob;
}

}  // namespace

// If this stops compiling, the field became a pb_callback_t and every sealed
// command silently stopped arriving on device.
static_assert(std::is_same<decltype(visio_schema_v1_control_SetRecordingKey{}
                                        .sealed),
                           visio_schema_v1_control_SetRecordingKey_sealed_t>::value,
              "SetRecordingKey.sealed must be a static byte array");

TEST(ControlNanopb, SetRecordingKeyRoundTripsAFullSizeEnvelope) {
  const std::string blob = MakeEnvelope(kSealedMax);

  visio_schema_v1_control_Command cmd =
      visio_schema_v1_control_Command_init_zero;
  cmd.which_body = visio_schema_v1_control_Command_set_recording_key_tag;
  auto& sealed = cmd.body.set_recording_key.sealed;
  ASSERT_EQ(sizeof(sealed.bytes), kSealedMax);
  sealed.size = static_cast<pb_size_t>(blob.size());
  std::memcpy(sealed.bytes, blob.data(), blob.size());

  const std::string wire =
      Encode(visio_schema_v1_control_Command_fields, cmd);

  visio_schema_v1_control_Command got =
      visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields, wire, &got));
  EXPECT_EQ(got.which_body,
            visio_schema_v1_control_Command_set_recording_key_tag);
  const auto& out = got.body.set_recording_key.sealed;
  ASSERT_EQ(out.size, blob.size());
  EXPECT_EQ(0, std::memcmp(out.bytes, blob.data(), blob.size()));
}

// nanopb does NOT truncate an oversized field — it fails pb_decode, so the
// device discards the WHOLE Command and answers nothing at all. That silence
// is why the generator refuses to emit an envelope over the cap.
TEST(ControlNanopb, AnOversizedSealedFieldFailsTheWholeDecode) {
  const std::string blob = MakeEnvelope(kSealedMax + 1);

  // Hand-encoded, because the static struct cannot hold an oversized field —
  // which is the point: only an off-device producer can create this.
  auto append_varint = [](std::string* out, std::size_t v) {
    while (v >= 0x80) {
      out->push_back(static_cast<char>((v & 0x7F) | 0x80));
      v >>= 7;
    }
    out->push_back(static_cast<char>(v));
  };

  std::string inner;                       // SetRecordingKey{ sealed = blob }
  append_varint(&inner, (1u << 3) | 2u);   // field 1, length-delimited
  append_varint(&inner, blob.size());
  inner += blob;

  std::string wire;                        // Command{ set_recording_key }
  append_varint(&wire, (39u << 3) | 2u);   // field 39, length-delimited
  append_varint(&wire, inner.size());
  wire += inner;

  visio_schema_v1_control_Command got =
      visio_schema_v1_control_Command_init_zero;
  EXPECT_FALSE(Decode(visio_schema_v1_control_Command_fields, wire, &got));
}

TEST(ControlNanopb, StorageSealedFieldsAreStaticAndAgree) {
  EXPECT_EQ(sizeof(visio_schema_v1_control_SetStorage{}.sealed.bytes),
            kSealedMax);
  EXPECT_EQ(sizeof(visio_schema_v1_control_TestStorage{}.sealed.bytes),
            kSealedMax);

  visio_schema_v1_control_Command cmd =
      visio_schema_v1_control_Command_init_zero;
  cmd.which_body = visio_schema_v1_control_Command_set_storage_tag;
  const std::string blob = MakeEnvelope(200);
  auto& s = cmd.body.set_storage.sealed;
  s.size = static_cast<pb_size_t>(blob.size());
  std::memcpy(s.bytes, blob.data(), blob.size());
  std::snprintf(cmd.body.set_storage.bucket,
                sizeof(cmd.body.set_storage.bucket), "gilabs-captures");

  const std::string wire =
      Encode(visio_schema_v1_control_Command_fields, cmd);
  visio_schema_v1_control_Command got =
      visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields, wire, &got));
  EXPECT_STREQ(got.body.set_storage.bucket, "gilabs-captures");
  ASSERT_EQ(got.body.set_storage.sealed.size, blob.size());
  EXPECT_EQ(0, std::memcmp(got.body.set_storage.sealed.bytes, blob.data(),
                           blob.size()));
}

// DeviceState reports the key fingerprint but never the key. 17 = 16 hex
// characters + NUL, the same 8 bytes the VREC container header carries.
TEST(ControlNanopb, DeviceStateCarriesTheFingerprintNotTheKey) {
  visio_schema_v1_control_DeviceState st =
      visio_schema_v1_control_DeviceState_init_zero;
  EXPECT_EQ(sizeof(st.recording_key_fingerprint), 17u);
  EXPECT_EQ(sizeof(st.seal_key_id), 9u);

  std::snprintf(st.recording_key_fingerprint,
                sizeof(st.recording_key_fingerprint), "730311a8e481b5e6");
  std::snprintf(st.seal_key_id, sizeof(st.seal_key_id), "77ba509d");
  st.recording_encryption_required = true;

  const std::string wire =
      Encode(visio_schema_v1_control_DeviceState_fields, st);
  visio_schema_v1_control_DeviceState got =
      visio_schema_v1_control_DeviceState_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_DeviceState_fields, wire, &got));
  EXPECT_STREQ(got.recording_key_fingerprint, "730311a8e481b5e6");
  EXPECT_STREQ(got.seal_key_id, "77ba509d");
  EXPECT_TRUE(got.recording_encryption_required);
}
