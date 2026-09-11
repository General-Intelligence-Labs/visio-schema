// Helpers shared by the McapWriter test files: temp paths, a dummy IMU
// channel, numbered-part paths, whole-file reads, and the VREC key/decrypt
// pair the encrypted comparisons need.
#pragma once

#include <gtest/gtest.h>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>

#include "visio_schema/mcap/recording_crypto.hpp"
#include "visio_schema/routing/channel.hpp"
#include "visio_schema/wire/message.hpp"

namespace mcap_test {

inline std::string TempPath(const std::string& name) {
  return (std::filesystem::temp_directory_path() / name).string();
}

inline visio_schema::Channel MakeChannel(std::uint32_t id,
                                         const std::string& topic) {
  visio_schema::Channel c;
  c.id = id;
  c.topic = topic;
  c.schema_name = "visio_schema.v1.sensor.ImuRaw";
  c.schema = std::string(8, '\x01');  // dummy FileDescriptorSet bytes
  return c;
}

inline visio_schema::wire::Message Data(std::uint32_t id,
                                        std::string payload) {
  visio_schema::wire::Message m;
  m.stream_id = id;
  m.payload = std::move(payload);
  return m;
}

// <stem>_NNNN.mcap under the temp dir, as the rotating writer names parts.
inline std::string PartPath(const std::string& stem_no_ext, int part) {
  char buf[16];
  std::snprintf(buf, sizeof(buf), "_%04d", part);
  return TempPath(stem_no_ext + buf + ".mcap");
}

inline std::string SlurpFile(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  return std::string((std::istreambuf_iterator<char>(f)),
                     std::istreambuf_iterator<char>());
}

// Parts of a rotated recording, in order, as far as they exist on disk.
inline int PartCount(const std::string& stem_no_ext) {
  int n = 0;
  while (std::filesystem::exists(PartPath(stem_no_ext, n))) ++n;
  return n;
}

inline std::uint64_t PartsBytes(const std::string& stem_no_ext) {
  std::uint64_t total = 0;
  for (int i = 0, n = PartCount(stem_no_ext); i < n; ++i)
    total += std::filesystem::file_size(PartPath(stem_no_ext, i));
  return total;
}

inline void RemoveParts(const std::string& stem_no_ext) {
  for (int i = 0, n = std::max(8, PartCount(stem_no_ext)); i < n; ++i)
    std::remove(PartPath(stem_no_ext, i).c_str());
}

// The 8-byte magic that closes every finalized part.
inline bool EndsWithMcapMagic(const std::string& part) {
  static const char kMagic[] = "\x89MCAP0\r\n";
  return part.size() >= 8 && part.compare(part.size() - 8, 8, kMagic, 8) == 0;
}

inline visio_schema::mcap::RecordingKey TestKey(std::uint8_t seed) {
  visio_schema::mcap::RecordingKey k{};
  for (std::size_t i = 0; i < k.size(); ++i)
    k[i] = static_cast<std::uint8_t>(seed + i);
  return k;
}

// The plaintext MCAP inside a VREC part (nonces differ per part, so
// encrypted parts are compared decrypted).
inline std::string DecryptPart(const std::string& raw,
                               const visio_schema::mcap::RecordingKey& key) {
  using namespace visio_schema::mcap;
  VrecHeader h;
  std::string err;
  EXPECT_TRUE(ParseVrecHeader(reinterpret_cast<const std::uint8_t*>(raw.data()),
                              raw.size(), &h, &err))
      << err;
  std::string body = raw.substr(kVrecHeaderBytes);
  RecordingCipher c(key, h.nonce);
  EXPECT_TRUE(c.valid());
  EXPECT_TRUE(c.XorAt(0, reinterpret_cast<const std::uint8_t*>(body.data()),
                      body.size(),
                      reinterpret_cast<std::uint8_t*>(body.data())));
  return body;
}

}  // namespace mcap_test
