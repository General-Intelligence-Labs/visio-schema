// Device-side (nanopb) round-trip for the sealed provisioning envelope.
//
// The `sealed` fields are `bytes`, and a bytes field with no max_size in
// nanopb.options becomes a pb_callback_t — which the firmware's static decode
// path cannot use, so the field would silently never arrive and a v2 settings
// QR would appear to apply while setting nothing. Everything asserted here is
// what makes the cap real on the device rather than a number in a text file.
#include <gtest/gtest.h>
#include "golden_vectors_test_util.hpp"
#include "visio_schema/wire/codec/frame.hpp"
#include "visio_schema/wire/message.hpp"

#include <pb_decode.h>
#include <pb_encode.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <type_traits>

#include "visio_schema/v1/control/command.pb.h"
#include "visio_schema/v1/control/command_result.pb.h"
#include "visio_schema/v1/service/snapshot/snapshot.pb.h"

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

TEST(ControlNanopb, JoinSavedWifiDecodesThePythonBoundaryVector) {
  const std::string ssid(32, 's');
  const std::string wire = std::string("\xf2\x02\x22\x0a\x20", 5) + ssid;
  visio_schema_v1_control_Command cmd =
      visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields, wire, &cmd));
  ASSERT_EQ(cmd.which_body,
            visio_schema_v1_control_Command_join_saved_wifi_tag);
  EXPECT_EQ(sizeof(cmd.body.join_saved_wifi.ssid), 33u);
  EXPECT_STREQ(cmd.body.join_saved_wifi.ssid, ssid.c_str());
  EXPECT_EQ(Encode(visio_schema_v1_control_Command_fields, cmd), wire);
}

TEST(ControlNanopb, JoinSavedWifiRejectsAnOversizedSsid) {
  const std::string wire = std::string("\xf2\x02\x23\x0a\x21", 5) +
                           std::string(33, 's');
  visio_schema_v1_control_Command cmd =
      visio_schema_v1_control_Command_init_zero;
  EXPECT_FALSE(Decode(visio_schema_v1_control_Command_fields, wire, &cmd));
}

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
  cmd.body.set_storage.has_bucket = true;  // presence: absent would keep
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

// An Ingest API destination rides the same SetStorage plus one optional
// hint; the device echoes the hint and says it can do the mode at all.
TEST(ControlNanopb, IngestDestinationHintRoundTrips) {
  visio_schema_v1_control_Command cmd =
      visio_schema_v1_control_Command_init_zero;
  cmd.which_body = visio_schema_v1_control_Command_set_storage_tag;
  auto& set = cmd.body.set_storage;
  set.has_endpoint_url = true;
  std::snprintf(set.endpoint_url, sizeof(set.endpoint_url),
                "visio+https://ingest.example.com/visio");
  set.has_destination_hint = true;
  std::snprintf(set.destination_hint, sizeof(set.destination_hint), "site-b");

  const std::string wire =
      Encode(visio_schema_v1_control_Command_fields, cmd);
  visio_schema_v1_control_Command got =
      visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields, wire, &got));
  EXPECT_TRUE(got.body.set_storage.has_destination_hint);
  EXPECT_STREQ(got.body.set_storage.destination_hint, "site-b");
  EXPECT_FALSE(got.body.set_storage.has_bucket);

  visio_schema_v1_control_DeviceState st =
      visio_schema_v1_control_DeviceState_init_zero;
  EXPECT_EQ(sizeof(st.storage_destination_hint),
            sizeof(visio_schema_v1_control_SetStorage{}.destination_hint));
  std::snprintf(st.storage_destination_hint,
                sizeof(st.storage_destination_hint), "site-b");
  st.ingest_api_supported = true;
  st.anti_flicker = visio_schema_v1_control_DeviceState_AntiFlicker_ANTI_FLICKER_50HZ;
  st.anti_flicker_pending = true;
  st.snapshot_supported = true;
  const std::string state =
      Encode(visio_schema_v1_control_DeviceState_fields, st);
  visio_schema_v1_control_DeviceState echoed =
      visio_schema_v1_control_DeviceState_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_DeviceState_fields, state,
                     &echoed));
  EXPECT_STREQ(echoed.storage_destination_hint, "site-b");
  EXPECT_TRUE(echoed.ingest_api_supported);
  EXPECT_EQ(echoed.anti_flicker, st.anti_flicker);
  EXPECT_TRUE(echoed.anti_flicker_pending);
  EXPECT_TRUE(echoed.snapshot_supported);
}

// The three Ingest commands are new arms of the Command / CommandResult
// unions the firmware decodes into static buffers. Their caps must not make
// either union larger than its existing largest arm.
static_assert(sizeof(visio_schema_v1_control_IngestOpenCredentials) <=
                  sizeof(visio_schema_v1_control_SetCalibration),
              "IngestOpenCredentials would grow every decoded Command");
static_assert(sizeof(visio_schema_v1_control_IngestSign) <=
                  sizeof(visio_schema_v1_control_SetCalibration),
              "IngestSign would grow every decoded Command");
static_assert(sizeof(visio_schema_v1_control_IngestCredentials) <=
                  sizeof(visio_schema_v1_control_DeviceState),
              "IngestCredentials would grow every CommandResult");
static_assert(std::is_array<decltype(
                  visio_schema_v1_control_IngestOpenCredentials{}
                      .sealed_credentials)>::value,
              "sealed_credentials must decode statically (needs max_size)");

template <std::size_t N>
void FillTo(char (&field)[N], char c) {
  std::memset(field, c, N - 1);
  field[N - 1] = '\0';
}

TEST(ControlNanopb, IngestCommandsRoundTripAtTheirCaps) {
  visio_schema_v1_control_Command sign =
      visio_schema_v1_control_Command_init_zero;
  sign.which_body = visio_schema_v1_control_Command_ingest_sign_tag;
  auto& s = sign.body.ingest_sign;
  s.has_path = s.has_query = s.has_body_sha256 = true;
  FillTo(s.path, '/');
  FillTo(s.query, 'q');
  FillTo(s.body_sha256, 'f');
  visio_schema_v1_control_Command got =
      visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields,
                     Encode(visio_schema_v1_control_Command_fields, sign),
                     &got));
  EXPECT_EQ(std::strlen(got.body.ingest_sign.path), sizeof(s.path) - 1);
  EXPECT_EQ(std::strlen(got.body.ingest_sign.query), sizeof(s.query) - 1);

  visio_schema_v1_control_Command open =
      visio_schema_v1_control_Command_init_zero;
  open.which_body = visio_schema_v1_control_Command_ingest_open_credentials_tag;
  auto& o = open.body.ingest_open_credentials;
  o.has_sealed_credentials = o.has_upload_id = true;
  FillTo(o.sealed_credentials, 'A');
  FillTo(o.upload_id, 'u');
  visio_schema_v1_control_Command got_open =
      visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields,
                     Encode(visio_schema_v1_control_Command_fields, open),
                     &got_open));
  EXPECT_EQ(
      std::strlen(got_open.body.ingest_open_credentials.sealed_credentials),
            sizeof(o.sealed_credentials) - 1);

  visio_schema_v1_control_Command check =
      visio_schema_v1_control_Command_init_zero;
  check.which_body = visio_schema_v1_control_Command_ingest_check_receipt_tag;
  auto& r = check.body.ingest_check_receipt;
  r.has_session_key = r.has_receipt = true;
  FillTo(r.session_key, 'k');
  FillTo(r.receipt, 'r');
  visio_schema_v1_control_Command got_check =
      visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields,
                     Encode(visio_schema_v1_control_Command_fields, check),
                     &got_check));
  EXPECT_STREQ(got_check.body.ingest_check_receipt.receipt, r.receipt);
}

TEST(ControlNanopb, IngestResultsRoundTripAtTheirCaps) {
  visio_schema_v1_control_CommandResult res =
      visio_schema_v1_control_CommandResult_init_zero;
  res.ok = true;
  res.which_payload =
      visio_schema_v1_control_CommandResult_ingest_credentials_tag;
  FillTo(res.payload.ingest_credentials.session_token, 't');
  FillTo(res.payload.ingest_credentials.secret_access_key, 's');
  visio_schema_v1_control_CommandResult got =
      visio_schema_v1_control_CommandResult_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_CommandResult_fields,
                     Encode(visio_schema_v1_control_CommandResult_fields, res),
                     &got));
  EXPECT_EQ(std::strlen(got.payload.ingest_credentials.session_token),
            sizeof(res.payload.ingest_credentials.session_token) - 1);

  res.which_payload = visio_schema_v1_control_CommandResult_ingest_signature_tag;
  FillTo(res.payload.ingest_signature.authorization, 'a');
  visio_schema_v1_control_CommandResult got_sig =
      visio_schema_v1_control_CommandResult_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_CommandResult_fields,
                     Encode(visio_schema_v1_control_CommandResult_fields, res),
                     &got_sig));
  EXPECT_STREQ(got_sig.payload.ingest_signature.authorization,
               res.payload.ingest_signature.authorization);
}

TEST(ControlNanopb, TestStorageCarriesTheDestinationHint) {
  visio_schema_v1_control_Command cmd =
      visio_schema_v1_control_Command_init_zero;
  cmd.which_body = visio_schema_v1_control_Command_test_storage_tag;
  cmd.body.test_storage.has_destination_hint = true;
  FillTo(cmd.body.test_storage.destination_hint, 'h');
  visio_schema_v1_control_Command got =
      visio_schema_v1_control_Command_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields,
                     Encode(visio_schema_v1_control_Command_fields, cmd),
                     &got));
  EXPECT_EQ(std::strlen(got.body.test_storage.destination_hint),
            sizeof(cmd.body.test_storage.destination_hint) - 1);
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


TEST(ControlNanopb, SnapshotCorpusAndFiniteStream) {
  const auto vectors = visio_golden::Load("snapshot_vectors.txt");
  ASSERT_EQ(vectors.size(), 8u);
  for (const auto& entry : vectors) {
    if (entry.first.find(".cmd") != std::string::npos) {
      visio_schema_v1_control_Command cmd = visio_schema_v1_control_Command_init_zero;
      ASSERT_TRUE(Decode(visio_schema_v1_control_Command_fields, entry.second, &cmd));
      EXPECT_EQ(Encode(visio_schema_v1_control_Command_fields, cmd), entry.second);
      EXPECT_STREQ(cmd.target_device, "GILABS-TEST");
    } else if (entry.first.find(".chunk") != std::string::npos) {
      visio_schema_v1_service_snapshot_SnapshotChunk chunk =
          visio_schema_v1_service_snapshot_SnapshotChunk_init_zero;
      ASSERT_TRUE(Decode(visio_schema_v1_service_snapshot_SnapshotChunk_fields, entry.second, &chunk));
      EXPECT_EQ(Encode(visio_schema_v1_service_snapshot_SnapshotChunk_fields, chunk), entry.second);
      EXPECT_EQ(sizeof(chunk.jpeg.bytes), 1024u);
      EXPECT_GT(chunk.jpeg.size, 0u);
    } else {
      visio_schema_v1_control_CommandResult result = visio_schema_v1_control_CommandResult_init_zero;
      ASSERT_TRUE(Decode(visio_schema_v1_control_CommandResult_fields, entry.second, &result));
      EXPECT_EQ(Encode(visio_schema_v1_control_CommandResult_fields, result), entry.second);
      pb_release(visio_schema_v1_control_CommandResult_fields, &result);
    }
  }
  visio_schema_v1_control_CommandResult result = visio_schema_v1_control_CommandResult_init_zero;
  ASSERT_TRUE(Decode(visio_schema_v1_control_CommandResult_fields, vectors.at("capture.result"), &result));
  ASSERT_EQ(result.which_payload, visio_schema_v1_control_CommandResult_snapshot_tag);
  EXPECT_EQ(result.payload.snapshot.snapshot_id, result.command_id);
  EXPECT_EQ(result.payload.snapshot.camera_index, 7u);
  EXPECT_EQ(result.payload.snapshot.capture_pts_us, 12345678901234567ull);
}

TEST(ControlNanopb, SnapshotStreamRejectsOversizeBytes) {
  visio_schema_v1_service_snapshot_SnapshotChunk chunk =
      visio_schema_v1_service_snapshot_SnapshotChunk_init_zero;
  chunk.jpeg.size = 1025;
  std::uint8_t buffer[2048];
  auto os = pb_ostream_from_buffer(buffer, sizeof(buffer));
  EXPECT_FALSE(pb_encode(&os, visio_schema_v1_service_snapshot_SnapshotChunk_fields, &chunk));
}
