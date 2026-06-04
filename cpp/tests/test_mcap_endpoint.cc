// McapEndpoint tests — resolver-based channel naming, drop-until-mapped,
// rotation. Mirrors python/visio/tests/test_mcap_endpoint.py at the
// behavioural level (full MCAP content checks live in the Python suite).
#include "visio_schema/transport/mcap_endpoint.hpp"

#include <gtest/gtest.h>

#include <cstdio>
#include <filesystem>
#include <string>
#include <unordered_map>

#include "visio_schema/routing/channel.hpp"
#include "visio_schema/routing/registry.hpp"
#include "visio_schema/wire/control.hpp"

using namespace visio_schema::transport;
using visio_schema::Channel;
using visio_schema::kDeviceInfo;
using visio_schema::kFirstDynamic;
using visio_schema::routing::ChannelRegistry;
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

TEST(McapEndpoint, RecordsResolvedChannel) {
  const std::string path = TempPath("visio_mcap_test_basic.mcap");
  std::remove(path.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  {
    McapEndpoint ep(path, resolve);
    ep.Write(Data(kFirstDynamic, "frame-0"));
    ep.Write(Data(kFirstDynamic, "frame-1"));
    ep.Close();
  }
  ASSERT_TRUE(fs::exists(path));
  EXPECT_GT(fs::file_size(path), 0u);
  std::remove(path.c_str());
}

TEST(McapEndpoint, BoundedQueueDropsOldestUntilDrained) {
  const std::string path = TempPath("visio_mcap_test_bounded.mcap");
  std::remove(path.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  {
    // drop-oldest, depth 4: writes accumulate in RAM (no OnTick yet) and the
    // queue never exceeds the bound, regardless of how many were offered.
    McapEndpoint ep(path, resolve, /*max_bytes=*/0, /*max_duration_s=*/0.0,
                    visio_schema::transport::WritePolicy::drop_oldest(4));
    for (int i = 0; i < 100; ++i) ep.Write(Data(kFirstDynamic, "f"));
    EXPECT_EQ(ep.pending_frames(), 4u);  // bounded by the policy
    ep.OnTick(0);                         // flush to disk
    EXPECT_EQ(ep.pending_frames(), 0u);
    ep.Close();
  }
  ASSERT_TRUE(fs::exists(path));
  EXPECT_GT(fs::file_size(path), 0u);
  std::remove(path.c_str());
}

TEST(McapEndpoint, RecordsDeviceInfoViaWellKnownChannel) {
  // A DeviceInfo message resolves (via a real ChannelRegistry) to the well-known
  // /device_info channel and is recorded. No C++ MCAP reader exists, so this
  // checks the registry-resolve → writer-accepts path produces a non-empty file;
  // full content round-trip lives in the Python suite.
  const std::string path = TempPath("visio_mcap_test_devinfo.mcap");
  std::remove(path.c_str());
  ChannelRegistry reg("ego");
  auto resolve = [&](std::uint32_t id) { return reg.Resolve(id); };
  {
    McapEndpoint ep(path, resolve);
    Message m = Data(kDeviceInfo, "announce-bytes");  // resolves to /device_info
    ep.Write(m);
    ep.Close();
  }
  ASSERT_TRUE(fs::exists(path));
  EXPECT_GT(fs::file_size(path), 0u);
  std::remove(path.c_str());
}

TEST(McapEndpoint, DropsUntilMapped) {
  const std::string path = TempPath("visio_mcap_test_drop.mcap");
  std::remove(path.c_str());
  auto resolve = [](std::uint32_t) -> const Channel* { return nullptr; };
  {
    McapEndpoint ep(path, resolve);
    ep.Write(Data(kFirstDynamic + 5, "x"));  // unmapped -> dropped, no crash
    ep.Close();
  }
  EXPECT_TRUE(fs::exists(path));  // a valid (empty) MCAP is still written
  std::remove(path.c_str());
}

TEST(McapEndpoint, RotatesByBytes) {
  const std::string path = TempPath("visio_mcap_test_rot.mcap");
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  const std::string p0 = TempPath("visio_mcap_test_rot_000.mcap");
  const std::string p1 = TempPath("visio_mcap_test_rot_001.mcap");
  std::remove(p0.c_str());
  std::remove(p1.c_str());
  {
    McapEndpoint ep(path, resolve, /*max_bytes=*/16);
    for (int i = 0; i < 4; ++i) ep.Write(Data(kFirstDynamic, std::string(10, 'a')));
    ep.Close();
  }
  EXPECT_TRUE(fs::exists(p0));
  EXPECT_TRUE(fs::exists(p1));  // rolled into a second part
  std::remove(p0.c_str());
  std::remove(p1.c_str());
}
