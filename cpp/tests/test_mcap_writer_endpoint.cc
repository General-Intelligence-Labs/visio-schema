// McapWriterEndpoint tests — resolver-based channel naming, drop-until-mapped,
// rotation. McapWriterEndpoint is an active object: Start() spawns the writer
// thread, Send() resolves+enqueues, Stop() drains+joins+finalizes the file. The
// file is only readable after Stop(), so every test records via Start→Send→Stop,
// then asserts on disk. Mirrors the Python McapWriter endpoint test at the
// behavioural level (full MCAP content checks live in the Python suite).
#include "visio_schema/mcap/writer_endpoint.hpp"

#include <gtest/gtest.h>

#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <filesystem>
#include <string>
#include <thread>
#include <unordered_map>

#include "visio_schema/routing/channel.hpp"
#include "visio_schema/routing/registry.hpp"
#include "visio_schema/wire/control.hpp"

using namespace visio_schema::mcap;
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

}  // namespace

TEST(McapWriterEndpoint, RecordsResolvedChannel) {
  const std::string path = TempPath("visio_mcap_test_basic.mcap");
  std::remove(path.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  {
    McapWriterEndpoint ep(path, resolve);
    ep.Start(nullptr, nullptr);
    ep.Send(Data(kFirstDynamic, "frame-0"));
    ep.Send(Data(kFirstDynamic, "frame-1"));
    ep.Stop();  // drains the queue, joins the writer, finalizes the file
    // A healthy recording must never latch: the Close() catch in Stop() would
    // otherwise fail a good session and the collector would kill recording.
    EXPECT_FALSE(ep.write_failed());
  }
  ASSERT_TRUE(fs::exists(path));
  EXPECT_GT(fs::file_size(path), 0u);
  std::remove(path.c_str());
}

TEST(McapWriterEndpoint, BoundedQueueShedsWhenOverBounded) {
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
    McapWriterEndpoint ep(path, resolve, /*max_bytes=*/0, /*max_duration_s=*/0.0,
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

TEST(McapWriterEndpoint, RecordsDeviceInfoViaWellKnownChannel) {
  // A DeviceInfo message resolves (via a real ChannelRegistry) to the well-known
  // /device_info channel and is recorded. No C++ MCAP reader exists, so this
  // checks the registry-resolve → writer-accepts path produces a non-empty file;
  // full content round-trip lives in the Python suite.
  const std::string path = TempPath("visio_mcap_test_devinfo.mcap");
  std::remove(path.c_str());
  ChannelRegistry reg("ego");
  auto resolve = [&](std::uint32_t id) { return reg.Resolve(id); };
  {
    McapWriterEndpoint ep(path, resolve);
    ep.Start(nullptr, nullptr);
    ep.Send(Data(kDeviceInfo, "announce-bytes"));  // resolves to /device_info
    ep.Stop();
  }
  ASSERT_TRUE(fs::exists(path));
  EXPECT_GT(fs::file_size(path), 0u);
  std::remove(path.c_str());
}

TEST(McapWriterEndpoint, DropsUntilMapped) {
  const std::string path = TempPath("visio_mcap_test_drop.mcap");
  std::remove(path.c_str());
  auto resolve = [](std::uint32_t) -> const Channel* { return nullptr; };
  {
    McapWriterEndpoint ep(path, resolve);
    ep.Start(nullptr, nullptr);
    ep.Send(Data(kFirstDynamic + 5, "x"));  // unmapped -> dropped, no crash
    ep.Stop();
  }
  EXPECT_TRUE(fs::exists(path));  // a valid (empty) MCAP is still written
  std::remove(path.c_str());
}

TEST(McapWriterEndpoint, BytesWrittenPassesThroughInnerWriter) {
  // The byte counter crosses the enqueue → writer-thread boundary, so it only
  // reflects sends that have drained. Assert after Stop() (drain+join complete,
  // Close() doesn't reset the counter) to keep it race-free.
  const std::string path = TempPath("visio_mcap_test_bytes.mcap");
  std::remove(path.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  {
    McapWriterEndpoint ep(path, resolve);
    EXPECT_EQ(ep.bytes_written(), 0u);          // nothing sent yet
    ep.Start(nullptr, nullptr);
    ep.Send(Data(kFirstDynamic, "hello"));       // 5 bytes
    ep.Send(Data(kFirstDynamic, "world!"));       // +6 bytes
    ep.Stop();                                    // drain + join + Close
    EXPECT_EQ(ep.bytes_written(), 11u);          // passthrough survives Stop
  }
  std::remove(path.c_str());
}

TEST(McapWriterEndpoint, BytesWrittenMonotonicAcrossRotation) {
  // With rotation, the inner McapWriter's per-part counter resets each roll but
  // bytes_written() must stay monotonic — the passthrough reads the lifetime total.
  const std::string path = TempPath("visio_mcap_test_bytes_rot.mcap");
  const std::string p0 = TempPath("visio_mcap_test_bytes_rot_0000.mcap");
  const std::string p1 = TempPath("visio_mcap_test_bytes_rot_0001.mcap");
  std::remove(p0.c_str());
  std::remove(p1.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  {
    McapWriterEndpoint ep(path, resolve, /*max_bytes=*/16);
    ep.Start(nullptr, nullptr);
    for (int i = 0; i < 4; ++i) ep.Send(Data(kFirstDynamic, std::string(10, 'a')));  // +10 each
    ep.Stop();
    EXPECT_EQ(ep.bytes_written(), 40u);   // 4 × 10, no reset at the roll
    EXPECT_FALSE(ep.write_failed());      // healthy rotation must not latch
  }
  ASSERT_TRUE(fs::exists(p1));             // confirm a rotation actually happened
  std::remove(p0.c_str());
  std::remove(p1.c_str());
}

TEST(McapWriterEndpoint, RotatesByBytes) {
  const std::string path = TempPath("visio_mcap_test_rot.mcap");
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  const std::string p0 = TempPath("visio_mcap_test_rot_0000.mcap");
  const std::string p1 = TempPath("visio_mcap_test_rot_0001.mcap");
  std::remove(p0.c_str());
  std::remove(p1.c_str());
  {
    McapWriterEndpoint ep(path, resolve, /*max_bytes=*/16);
    ep.Start(nullptr, nullptr);
    for (int i = 0; i < 4; ++i) ep.Send(Data(kFirstDynamic, std::string(10, 'a')));
    ep.Stop();
  }
  EXPECT_TRUE(fs::exists(p0));
  EXPECT_TRUE(fs::exists(p1));  // rolled into a second part
  std::remove(p0.c_str());
  std::remove(p1.c_str());
}

// A storage failure mid-recording must NOT escape the writer thread. It is the
// thread's entry function, so an exception leaving it calls std::terminate and
// kills the whole process — on a capture rig that means losing the bus and the
// cameras too. Observed in the field: a corrupt FAT card was flipped read-only
// by the kernel mid-recording, the next part rotation threw out of OpenPart,
// and the firmware aborted. Here the directory is made unwritable AFTER the
// first part opens, so the rotation fails exactly the same way.
TEST(McapWriterEndpoint, StorageFailureMidRecordingIsLatchedNotThrown) {
  if (::geteuid() == 0) GTEST_SKIP() << "perm bits don't block writes as root";
  const fs::path dir = fs::temp_directory_path() / "visio_mcap_ro_test";
  fs::remove_all(dir);
  fs::create_directories(dir);
  const std::string path = (dir / "rec.mcap").string();

  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };

  bool failed = false;
  std::uint64_t dropped = 0;
  {
    McapWriterEndpoint ep(path, resolve, /*max_bytes=*/16);   // rotates fast
    ep.Start(nullptr, nullptr);
    EXPECT_FALSE(ep.write_failed());          // healthy before the card dies
    ep.Send(Data(kFirstDynamic, std::string(10, 'a')));

    // The card "goes read-only": part 0 is already open, the next OpenPart fails.
    fs::permissions(dir, fs::perms::owner_read | fs::perms::owner_exec,
                    fs::perm_options::replace);
    for (int i = 0; i < 8; ++i) ep.Send(Data(kFirstDynamic, std::string(10, 'a')));

    for (int i = 0; i < 200 && !failed; ++i) {   // writer thread is async
      failed = ep.write_failed();
      if (!failed) std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    // Send() must stay well-behaved afterwards, and the latch must be sticky:
    // frames offered to a dead endpoint are shed, not written, and not fatal.
    for (int i = 0; i < 4; ++i) ep.Send(Data(kFirstDynamic, std::string(10, 'a')));
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    EXPECT_TRUE(ep.write_failed()) << "latch cleared";
    dropped = ep.dropped_frames();
    ep.Stop();
  }
  fs::permissions(dir, fs::perms::owner_all, fs::perm_options::replace);

  EXPECT_TRUE(failed) << "storage failure was not latched";
  EXPECT_GT(dropped, 0u) << "post-failure frames were not shed";
  fs::remove_all(dir);
}

// The same failure, but torn down WITHOUT an explicit Stop(). Stop() early-outs
// on its stop_ guard, so a test that calls it leaves the destructor's own
// join+Close path — the one a noexcept destructor runs on dead storage —
// completely unexercised.
TEST(McapWriterEndpoint, StorageFailureSurvivesDestructorWithoutExplicitStop) {
  if (::geteuid() == 0) GTEST_SKIP() << "perm bits don't block writes as root";
  const fs::path dir = fs::temp_directory_path() / "visio_mcap_ro_dtor_test";
  fs::remove_all(dir);
  fs::create_directories(dir);
  const std::string path = (dir / "rec.mcap").string();

  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };

  bool failed = false;
  {
    McapWriterEndpoint ep(path, resolve, /*max_bytes=*/16);
    ep.Start(nullptr, nullptr);
    ep.Send(Data(kFirstDynamic, std::string(10, 'a')));
    fs::permissions(dir, fs::perms::owner_read | fs::perms::owner_exec,
                    fs::perm_options::replace);
    for (int i = 0; i < 8; ++i) ep.Send(Data(kFirstDynamic, std::string(10, 'a')));
    for (int i = 0; i < 200 && !failed; ++i) {
      failed = ep.write_failed();
      if (!failed) std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
  }  // <-- destructor runs Stop() for real here; must not terminate
  fs::permissions(dir, fs::perms::owner_all, fs::perm_options::replace);

  EXPECT_TRUE(failed);
  fs::remove_all(dir);
}
