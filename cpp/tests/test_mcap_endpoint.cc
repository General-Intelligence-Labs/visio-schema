// McapEndpoint tests — resolver-based channel naming, drop-until-mapped,
// rotation. McapEndpoint is an active object: Start() spawns the writer thread,
// Send() resolves+enqueues, Stop() drains+joins+finalizes the file. The file is
// only readable after Stop(), so every test records via Start→Send→Stop, then
// asserts on disk. Mirrors python/visio/tests/test_mcap_endpoint.py at the
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
    ep.Start(nullptr, nullptr);
    ep.Send(Data(kFirstDynamic, "frame-0"));
    ep.Send(Data(kFirstDynamic, "frame-1"));
    ep.Stop();  // drains the queue, joins the writer, finalizes the file
  }
  ASSERT_TRUE(fs::exists(path));
  EXPECT_GT(fs::file_size(path), 0u);
  std::remove(path.c_str());
}

TEST(McapEndpoint, BoundedQueueShedsWhenOverBounded) {
  const std::string path = TempPath("visio_mcap_test_bounded.mcap");
  std::remove(path.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  std::uint64_t dropped = 0;
  {
    // drop-oldest, tiny depth: a large burst is offered faster than the writer
    // thread can drain it, so the bounded queue sheds the oldest frames. We
    // assert that drops happened (robust inequality, not an exact count — the
    // writer drains concurrently so the precise number is timing-dependent).
    McapEndpoint ep(path, resolve, /*max_bytes=*/0, /*max_duration_s=*/0.0,
                    visio_schema::transport::WritePolicy::drop_oldest(4));
    ep.Start(nullptr, nullptr);
    for (int i = 0; i < 100000; ++i) ep.Send(Data(kFirstDynamic, "f"));
    dropped = ep.dropped_frames();
    ep.Stop();  // flush whatever survived + finalize
  }
  EXPECT_GT(dropped, 0u);  // the tiny bound shed frames under the burst
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
    ep.Start(nullptr, nullptr);
    ep.Send(Data(kDeviceInfo, "announce-bytes"));  // resolves to /device_info
    ep.Stop();
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
    ep.Start(nullptr, nullptr);
    ep.Send(Data(kFirstDynamic + 5, "x"));  // unmapped -> dropped, no crash
    ep.Stop();
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
    ep.Start(nullptr, nullptr);
    for (int i = 0; i < 4; ++i) ep.Send(Data(kFirstDynamic, std::string(10, 'a')));
    ep.Stop();
  }
  EXPECT_TRUE(fs::exists(p0));
  EXPECT_TRUE(fs::exists(p1));  // rolled into a second part
  std::remove(p0.c_str());
  std::remove(p1.c_str());
}
