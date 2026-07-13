// McapWriter tests — writes a non-empty MCAP from (Channel, Message) pairs and
// rotates into numbered parts. Behavioural level; full MCAP content + Foxglove
// readability checks live in the Python suite (test_recording.py,
// test_mcap_foxglove_e2e.py).
#include "visio_schema/mcap/writer.hpp"

#include <gtest/gtest.h>

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>

#if defined(__linux__)
#include <climits>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#endif

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

#if defined(__linux__)
// The open part file's fd must be close-on-exec. Otherwise it is inherited by
// every child this process fork+execs (notably the long-lived Wi-Fi AP daemons
// spawned via posix_spawn), and that inherited fd keeps the SD card busy for the
// daemon's whole lifetime — so a later unmount, and thus the "format SD card"
// command, fails with EBUSY. Regression test for the O_CLOEXEC part open.
TEST(McapWriter, PartFileIsCloexec) {
  const std::string path = TempPath("visio_schema_mcap_cloexec.mcap");
  std::remove(path.c_str());
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  McapWriter w(path);
  w.Write(ch, Data(kFirstDynamic, "x"));  // part file is open at this point

  const std::string want = fs::path(path).filename().string();
  bool found = false;
  if (DIR* d = opendir("/proc/self/fd")) {
    for (struct dirent* e; (e = readdir(d)) != nullptr;) {
      if (e->d_name[0] == '.') continue;
      const std::string link = std::string("/proc/self/fd/") + e->d_name;
      char target[PATH_MAX];
      const ssize_t n = readlink(link.c_str(), target, sizeof(target) - 1);
      if (n <= 0) continue;
      target[n] = '\0';
      if (fs::path(target).filename().string() != want) continue;
      const int fd = std::atoi(e->d_name);
      const int flags = fcntl(fd, F_GETFD);
      ASSERT_GE(flags, 0);
      EXPECT_TRUE(flags & FD_CLOEXEC)
          << "recording part fd must be O_CLOEXEC (not inherited by children)";
      found = true;
    }
    closedir(d);
  }
  EXPECT_TRUE(found) << "open part fd not found in /proc/self/fd";
  w.Close();
  std::remove(path.c_str());
}
#endif  // __linux__

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

TEST(McapWriter, BytesWrittenAdvancesByPayloadSize) {
  const std::string path = TempPath("visio_schema_mcap_bytes.mcap");
  std::remove(path.c_str());
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  McapWriter w(path);
  EXPECT_EQ(w.bytes_written(), 0u);
  w.Write(ch, Data(kFirstDynamic, "hello"));       // 5 bytes
  EXPECT_EQ(w.bytes_written(), 5u);
  w.Write(ch, Data(kFirstDynamic, "world!"));       // +6 bytes
  EXPECT_EQ(w.bytes_written(), 11u);
  w.Close();
  EXPECT_EQ(w.bytes_written(), 11u);                // Close doesn't reset it
  std::remove(path.c_str());
}

TEST(McapWriter, BytesWrittenIsMonotonicAcrossRotation) {
  const std::string path = TempPath("visio_schema_mcap_bytes_rot.mcap");
  const std::string p0 = TempPath("visio_schema_mcap_bytes_rot_0000.mcap");
  const std::string p1 = TempPath("visio_schema_mcap_bytes_rot_0001.mcap");
  std::remove(p0.c_str());
  std::remove(p1.c_str());
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  {
    // max_bytes=16 forces a roll partway through; bytes_written() must keep
    // climbing across the part boundary even though part_bytes_ resets.
    McapWriter w(path, /*max_bytes=*/16);
    std::uint64_t last = 0;
    for (int i = 0; i < 4; ++i) {
      w.Write(ch, Data(kFirstDynamic, std::string(10, 'a')));  // +10 each
      EXPECT_GT(w.bytes_written(), last);                       // strictly increasing
      last = w.bytes_written();
    }
    EXPECT_EQ(w.bytes_written(), 40u);   // 4 × 10, no reset at the roll
    w.Close();
  }
  ASSERT_TRUE(fs::exists(p1));           // confirm a rotation actually happened
  std::remove(p0.c_str());
  std::remove(p1.c_str());
}
