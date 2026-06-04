// McapWriter tests — writes a non-empty MCAP from (Channel, Message) pairs and
// rotates into numbered parts. Behavioural level; full MCAP content + Foxglove
// readability checks live in the Python suite (test_recording.py,
// test_mcap_foxglove_e2e.py).
#include "visio_schema/mcap/writer.hpp"

#include <gtest/gtest.h>

#include <cstdio>
#include <filesystem>
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
  c.schema_name = "visio_schema.sensor.v1.ImuRaw";
  c.schema = std::string(8, '\x01');  // dummy FileDescriptorSet bytes
  return c;
}

Message Data(std::uint32_t id, std::string payload) {
  Message m;
  m.stream_id = id;
  m.payload = std::move(payload);
  return m;
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
  const std::string p0 = TempPath("visio_schema_mcap_rot_000.mcap");
  const std::string p1 = TempPath("visio_schema_mcap_rot_001.mcap");
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
