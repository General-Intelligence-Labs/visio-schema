// McapWriter tests — writes a non-empty MCAP from (Channel, Message) pairs and
// rotates into numbered parts. Behavioural level; full MCAP content + Foxglove
// readability checks live in the Python suite (test_recording.py,
// test_mcap_foxglove_e2e.py).
#include "visio_schema/mcap/writer.hpp"

#include <gtest/gtest.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>

#include "visio_schema/routing/channel.hpp"
#include "visio_schema/wire/control.hpp"

using visio_schema::Channel;
using visio_schema::kFirstDynamic;
using visio_schema::mcap::McapWriter;
using visio_schema::wire::Message;
namespace fs = std::filesystem;

namespace {

std::string TempPath(const std::string& name) {
  return (fs::temp_directory_path() / name).string();
}

Channel MakeChannel(std::uint32_t id, const std::string& topic) {
  Channel c;
  c.id = id;
  c.topic = topic;
  c.schema_name = "visio_schema.v1.sensor.ImuRaw";
  c.schema = std::string(8, '\x01');  // dummy FileDescriptorSet bytes
  return c;
}

Message Data(std::uint32_t id, std::string payload) {
  Message m;
  m.stream_id = id;
  m.payload = std::move(payload);
  return m;
}

// Compression is None, so a message's payload bytes appear verbatim in the
// file — substring presence is a valid containment check.
std::string ReadAll(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  std::ostringstream ss;
  ss << in.rdbuf();
  return ss.str();
}

bool Contains(const std::string& haystack, const std::string& needle) {
  return haystack.find(needle) != std::string::npos;
}

}  // namespace

TEST(McapWriter, WritesNonEmptyFile) {
  const std::string path = TempPath("visio_schema_mcap_basic.mcap");
  std::remove(path.c_str());
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  {
    McapWriter w(path);
    w.Write(ch, Data(kFirstDynamic, "frame-0"));
    w.Write(ch, Data(kFirstDynamic, "frame-1"));
    w.Close();
  }
  ASSERT_TRUE(fs::exists(path));
  EXPECT_GT(fs::file_size(path), 0u);
  std::remove(path.c_str());
}

TEST(McapWriter, DestructorFinalizes) {
  const std::string path = TempPath("visio_schema_mcap_dtor.mcap");
  std::remove(path.c_str());
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  {
    McapWriter w(path);
    w.Write(ch, Data(kFirstDynamic, "x"));
    // no explicit Close(): the destructor must flush + close.
  }
  ASSERT_TRUE(fs::exists(path));
  EXPECT_GT(fs::file_size(path), 0u);
  std::remove(path.c_str());
}

TEST(McapWriter, RotatesByBytesIntoNumberedParts) {
  const std::string path = TempPath("visio_schema_mcap_rot.mcap");
  const std::string p0 = TempPath("visio_schema_mcap_rot_0000.mcap");
  const std::string p1 = TempPath("visio_schema_mcap_rot_0001.mcap");
  std::remove(p0.c_str());
  std::remove(p1.c_str());
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  {
    McapWriter w(path, /*max_bytes=*/16);
    for (int i = 0; i < 4; ++i) w.Write(ch, Data(kFirstDynamic, std::string(10, 'a')));
    w.Close();
  }
  EXPECT_TRUE(fs::exists(p0));
  EXPECT_TRUE(fs::exists(p1));   // rolled into a second part
  std::remove(p0.c_str());
  std::remove(p1.c_str());
}

TEST(McapWriter, PreambleHeadsEveryPart) {
  const std::string path = TempPath("visio_schema_mcap_pre.mcap");
  const std::string p0 = TempPath("visio_schema_mcap_pre_0000.mcap");
  const std::string p1 = TempPath("visio_schema_mcap_pre_0001.mcap");
  std::remove(p0.c_str());
  std::remove(p1.c_str());
  const Channel data_ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  const Channel calib_ch =
      MakeChannel(kFirstDynamic + 1, "/dev/camera/0/intrinsics");
  {
    McapWriter w(path, /*max_bytes=*/64);
    w.SetPreamble({{calib_ch, Data(kFirstDynamic + 1, "CALIB-PREAMBLE")}});
    // 40-byte frames: two land in part 0 (preamble bytes count toward the cap),
    // the next rolls — every rotated part must replay the preamble at its head.
    for (int i = 0; i < 3; ++i)
      w.Write(data_ch, Data(kFirstDynamic, "data-" + std::to_string(i) +
                                                std::string(35, 'x')));
    w.Close();
  }
  ASSERT_TRUE(fs::exists(p0));
  ASSERT_TRUE(fs::exists(p1));
  const std::string part0 = ReadAll(p0);
  const std::string part1 = ReadAll(p1);
  EXPECT_TRUE(Contains(part0, "CALIB-PREAMBLE"));
  EXPECT_TRUE(Contains(part1, "CALIB-PREAMBLE"));  // replayed on rotation
  EXPECT_TRUE(Contains(part0, "data-0"));
  EXPECT_TRUE(Contains(part1, "data-2"));
  // The preamble precedes the data within its part.
  EXPECT_LT(part1.find("CALIB-PREAMBLE"), part1.find("data-2"));
  std::remove(p0.c_str());
  std::remove(p1.c_str());
}

TEST(McapWriter, PreambleAloneNeverRolls) {
  // Preamble bigger than max_bytes: writing it must not trigger rotation (which
  // would replay the preamble and recurse) — a part holding only its preamble
  // counts as empty for the roll check.
  const std::string path = TempPath("visio_schema_mcap_pre_only.mcap");
  const std::string p0 = TempPath("visio_schema_mcap_pre_only_0000.mcap");
  const std::string p1 = TempPath("visio_schema_mcap_pre_only_0001.mcap");
  std::remove(p0.c_str());
  std::remove(p1.c_str());
  const Channel data_ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  const Channel calib_ch =
      MakeChannel(kFirstDynamic + 1, "/dev/camera/0/intrinsics");
  {
    McapWriter w(path, /*max_bytes=*/8);
    w.SetPreamble(
        {{calib_ch, Data(kFirstDynamic + 1, "CALIB-PREAMBLE-OVER-CAP")}});
    w.Write(data_ch, Data(kFirstDynamic, "first"));  // stays in part 0
    w.Close();
  }
  ASSERT_TRUE(fs::exists(p0));
  EXPECT_FALSE(fs::exists(p1));  // preamble alone never rolled
  const std::string part0 = ReadAll(p0);
  EXPECT_TRUE(Contains(part0, "CALIB-PREAMBLE-OVER-CAP"));
  EXPECT_TRUE(Contains(part0, "first"));
  std::remove(p0.c_str());
}
