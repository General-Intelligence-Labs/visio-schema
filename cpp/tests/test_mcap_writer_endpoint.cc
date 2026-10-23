// McapWriterEndpoint tests — resolver-based channel naming, drop-until-mapped,
// rotation. McapWriterEndpoint is an active object: Start() spawns the writer
// thread, Send() resolves+enqueues, Stop() drains+joins+finalizes the file. The
// file is only readable after Stop(), so every test records via Start→Send→Stop,
// then asserts on disk. Mirrors the Python McapWriter endpoint test at the
// behavioural level (full MCAP content checks live in the Python suite).
#include "visio_schema/mcap/writer_endpoint.hpp"

#include <gtest/gtest.h>
#include <atomic>

#include <unistd.h>
#include <time.h>

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cerrno>
#include <filesystem>
#include <string>
#include <thread>
#include <memory>
#include <unordered_map>
#include <vector>

#include "mcap_writer_test_util.hpp"
#include "visio_schema/routing/channel.hpp"
#include "visio_schema/routing/registry.hpp"
#include "visio_schema/transport/stream_policy.hpp"
#include "visio_schema/wire/control.hpp"

using namespace visio_schema::mcap;
using namespace visio_schema::transport;
using namespace mcap_test;
using visio_schema::Channel;
using visio_schema::mcap::RecordingKey;
using visio_schema::mcap::McapReadbackStats;
using visio_schema::kCommand;
using visio_schema::kDeviceInfo;
using visio_schema::kDiag;
using visio_schema::kFirstDynamic;
using visio_schema::kHeartbeat;
using visio_schema::routing::ChannelRegistry;
using visio_schema::wire::Message;
namespace fs = std::filesystem;

namespace {
void ExpectIdleCpu() {
  timespec before{}, after{};
  ASSERT_EQ(clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &before), 0);
  std::this_thread::sleep_for(std::chrono::milliseconds(200));
  ASSERT_EQ(clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &after), 0);
  const double cpu_s = after.tv_sec - before.tv_sec +
                       (after.tv_nsec - before.tv_nsec) / 1e9;
  EXPECT_LT(cpu_s, 0.05) << "empty recorder consumed CPU instead of sleeping";
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

TEST(McapWriterEndpoint, StatsExposeDropsAndQueueWatermark) {
  const std::string path = TempPath("visio_mcap_test_stats_drop.mcap");
  std::remove(path.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  {
    // Byte-bounded queue under a burst: stats() must mirror dropped_frames(),
    // and the enqueue watermark can never exceed the policy's byte bound —
    // that distance is the margin claim callers log, so it has to be honest.
    McapWriterEndpoint ep(path, resolve, /*max_bytes=*/0, /*max_duration_s=*/0.0,
                    visio_schema::transport::WritePolicy::stale_eviction(
                        256, std::chrono::microseconds(0)));
    ep.Start(nullptr, nullptr);
    for (int i = 0; i < 100000; ++i)
      ep.Send(Data(kFirstDynamic, "0123456789abcdef"));
    const McapWriterStats st = ep.stats();
    EXPECT_GT(st.dropped, 0u);
    EXPECT_EQ(st.dropped, ep.dropped_frames());
    EXPECT_GT(st.max_pending_bytes, 0u);
    EXPECT_LE(st.max_pending_bytes, 256u);
    ep.Stop();
  }
  std::remove(path.c_str());
}

// The queue's byte bound has to bound what is RESIDENT, not merely what is
// queued. The writer thread swaps the whole queue into a batch per wake, so for
// that batch's lifetime a bound's worth of frames is held twice over unless the
// drain releases each entry as it writes it — on the device that was 16 MiB
// becoming 32 MiB on a 44 MB box.
//
// Two things are pinned, both from OUTSIDE the endpoint. The payload buffers
// are really freed through the drain: each message's bytes are handed over as a
// shared buffer this test keeps its own reference to, so use_count() drops to
// 1 exactly when the endpoint lets go — accounting alone cannot fake it. And
// pending_bytes() stays honest across the swap, where it used to read 0 for the
// whole time a batch was being written.
TEST(McapWriterEndpoint, BatchIsFreedAsItDrainsNotAtTheEnd) {
  const std::string path = TempPath("visio_mcap_test_drain_frees.mcap");
  std::remove(path.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  // Big enough that the drain is real I/O the sampler below can watch (~8 MB,
  // the scale one device part is written at), and framed as ONE batch: every
  // frame is queued before the writer thread exists.
  constexpr std::size_t kFrames = 64;
  constexpr std::size_t kFrameBytes = 128 * 1024;
  std::vector<std::shared_ptr<const std::string>> payloads;
  {
    McapWriterEndpoint ep(path, resolve);  // lossless: nothing is shed
    for (std::size_t i = 0; i < kFrames; ++i) {
      payloads.push_back(std::make_shared<const std::string>(
          kFrameBytes, static_cast<char>('a' + i % 26)));
      Message m;
      m.stream_id = kFirstDynamic;
      m.payload = visio_schema::wire::Payload(payloads.back());
      ep.Send(m);
    }
    ASSERT_EQ(ep.pending_frames(), kFrames);
    ASSERT_EQ(ep.pending_bytes(), kFrames * kFrameBytes);  // exact before a swap

    std::size_t mid_drain_samples = 0;
    std::size_t most_freed = 0;
    std::size_t least_pending = kFrames * kFrameBytes;
    ep.Start(nullptr, nullptr);
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(20);
    for (;;) {
      const std::uint64_t writes = ep.stats().writes;
      if (writes >= kFrames) break;
      if (std::chrono::steady_clock::now() > deadline) break;
      std::this_thread::yield();  // never starve the writer off a single core
      if (writes == 0) continue;  // the batch has not been taken yet
      ++mid_drain_samples;
      std::size_t freed = 0;
      for (const auto& p : payloads) freed += (p.use_count() == 1);
      most_freed = std::max(most_freed, freed);
      least_pending = std::min(least_pending, ep.pending_bytes());
    }
    ep.Stop();
    ASSERT_GT(mid_drain_samples, 0u)
        << "the drain outran the sampler — raise kFrames/kFrameBytes";
    // Both are 0 and kFrames*kFrameBytes respectively under a drain that holds
    // the batch to a trailing clear(): nothing is freed until the last write.
    EXPECT_GT(most_freed, 0u) << "no payload was released mid-batch";
    EXPECT_LT(least_pending, kFrames * kFrameBytes)
        << "pending_bytes() does not fall as the batch drains";
    EXPECT_EQ(ep.pending_bytes(), 0u);      // and it lands on zero
    EXPECT_EQ(ep.stats().dropped, 0u);      // a lossless queue sheds nothing
    EXPECT_EQ(ep.bytes_written(), kFrames * kFrameBytes);
  }
  for (const auto& p : payloads)
    EXPECT_EQ(p.use_count(), 1) << "the endpoint outlived by a payload";
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
    // Written, not merely accepted: an empty MCAP is also non-empty on disk.
    EXPECT_EQ(ep.bytes_written(), 14u);
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
    ep.Send(Data(kFirstDynamic + 6, "y"));  // a SECOND missing topic
    ep.Stop();
    // The counter exists to separate this from a queue drop. Storage was never
    // the problem here — whole topics are simply absent — and folding the two
    // together would hide exactly the failure it was added to surface.
    EXPECT_EQ(ep.unmapped_frames(), 2u);
    EXPECT_EQ(ep.stats().unmapped, 2u);
    EXPECT_EQ(ep.stats().dropped, 0u);
  }
  EXPECT_TRUE(fs::exists(path));  // a valid (empty) MCAP is still written
  std::remove(path.c_str());
}

TEST(McapWriterEndpoint, ControlStreamsAreNotCountedUnmapped) {
  // A bus fans its heartbeat to every peer, sinks included. Control ids must
  // not count as unmapped, or the missing-topic warning fires on every
  // recording and hides a real one.
  const std::string path = TempPath("visio_mcap_test_control.mcap");
  std::remove(path.c_str());
  auto resolve = [](std::uint32_t) -> const Channel* { return nullptr; };
  {
    McapWriterEndpoint ep(path, resolve);
    ep.Start(nullptr, nullptr);
    for (int i = 0; i < 3; ++i) ep.Send(Data(kHeartbeat, "beat"));
    ep.Send(Data(kCommand, "cmd"));
    ep.Send(Data(kDiag, "diag"));
    ep.Send(Data(kFirstDynamic + 5, "x"));  // a data topic that IS missing
    ep.Stop();
    EXPECT_EQ(ep.stats().unmapped, 1u);
    EXPECT_EQ(ep.stats().dropped, 0u);
  }
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

// A live viewer can thin its own feed with a stream policy (SetStreamPolicy).
// The recorder must be deaf to that: a preview showing one camera must not
// silently produce a one-camera recording. McapWriterEndpoint derives straight
// from Endpoint and does NOT override SetStreamPolicy, so it takes the base
// no-op — this pins that, by handing it the most hostile policy possible and
// checking every message still lands.
TEST(McapWriterEndpoint, IgnoresAStreamPolicyAndKeepsRecordingEverything) {
  const std::string path = TempPath("visio_mcap_test_policy.mcap");
  std::remove(path.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/camera/0")},
      {kFirstDynamic + 1, MakeChannel(kFirstDynamic + 1, "/dev/camera/1")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };

  // Drop both streams outright — what a preview-only client would install.
  auto policy = std::make_shared<const ResolvedStreamPolicy>(
      ResolvedStreamPolicy{{kFirstDynamic, StreamRule{true, 0}},
                           {kFirstDynamic + 1, StreamRule{true, 0}}});

  std::uint64_t written = 0;
  {
    McapWriterEndpoint ep(path, resolve);
    ep.Start(nullptr, nullptr);
    ep.SetStreamPolicy(policy);
    ep.Send(Data(kFirstDynamic, "left-frame"));
    ep.Send(Data(kFirstDynamic + 1, "right-frame"));
    ep.Stop();
    EXPECT_FALSE(ep.write_failed());
    written = ep.bytes_written();
  }
  // Both payloads reached disk: the filter is per-endpoint, and this endpoint
  // never opted in.
  EXPECT_EQ(written, std::string("left-frame").size() +
                         std::string("right-frame").size());
  ASSERT_TRUE(fs::exists(path));
  std::remove(path.c_str());
}

TEST(McapWriterEndpoint, DeliversWhileRunningWithoutStopAsTheFlush) {
  // Guards the empty→non-empty notify edge in Send(): a writer asleep with a
  // non-empty queue would be rescued by Stop()'s unconditional notify, and
  // every other test in this file asserts only after Stop() — so a lost
  // wakeup would be silently flushed away there. This one polls
  // bytes_written() BEFORE Stop, across several bursts so the edge is
  // crossed repeatedly (the writer drains fully between bursts).
  const std::string path = TempPath("visio_mcap_test_live.mcap");
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
    std::uint64_t want = 0;
    for (int burst = 0; burst < 5; ++burst) {
      for (int i = 0; i < 20; ++i) {
        ep.Send(Data(kFirstDynamic, "0123456789"));
        want += 10;
      }
      for (int spin = 0; spin < 2000 && ep.bytes_written() < want; ++spin) {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
      }
      ASSERT_EQ(ep.bytes_written(), want)
          << "writer asleep with a non-empty queue (burst " << burst << ")";
    }
    ep.Stop();
  }
  std::remove(path.c_str());
}

// Write-time read-back passthrough: the options reach the inner writer, the
// step is driven from a thread that is NOT the writer thread (this one),
// and Stop() runs the bounded close flush so the final tail is verified.
TEST(McapWriterEndpoint, ReadbackPassthrough) {
  const std::string path = TempPath("visio_mcap_test_readback.mcap");
  std::remove(path.c_str());
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  McapReadbackOptions rb;
  rb.ring_bytes = 4 << 20;
  rb.settle_bytes = 0;
  rb.close_flush_ms = 5000;
  {
    McapWriterEndpoint ep(path, resolve, /*max_bytes=*/0,
                          /*max_duration_s=*/0.0, WritePolicy::lossless(),
                          {}, false, 0, /*sync_span_bytes=*/64 * 1024,
                          std::nullopt, rb);
    ep.Start(nullptr, nullptr);
    for (int i = 0; i < 512; ++i) {
      ep.Send(Data(kFirstDynamic, std::string(3000, char('a' + i % 26))));
      if (i % 64 == 0) ep.ReadbackStep(std::chrono::milliseconds(20));
    }
    ep.Stop();
    EXPECT_EQ(ep.readback_pending(), 0u);
    const McapReadbackStats st = ep.readback_stats();
    EXPECT_EQ(st.bytes_verified, fs::file_size(path));  // spans + tail
    EXPECT_EQ(st.spans_skipped, 0u);
    EXPECT_EQ(st.spans_mismatched, 0u);
    EXPECT_FALSE(ep.storage_fault());
    EXPECT_FALSE(ep.write_failed());
  }
  std::remove(path.c_str());
}

// The one concurrent shape the library must survive: the writer thread
// overrunning the ring while a stepper spins on it. A compare torn by the
// writer is discarded, never mistaken for a mismatch — so with the bytes
// intact there is never a fault, whatever the ring size, and the parts
// are what read-back-off produces.
TEST(McapWriterEndpoint, ReadbackOverrunUnderAConcurrentStepperNeverFaults) {
  const RecordingKey key = TestKey(21);  // VREC: 64 KiB spans
  std::unordered_map<std::uint32_t, Channel> table{
      {kFirstDynamic, MakeChannel(kFirstDynamic, "/dev/imu/0/raw")}};
  auto resolve = [&](std::uint32_t id) -> const Channel* {
    auto it = table.find(id);
    return it == table.end() ? nullptr : &it->second;
  };
  auto record = [&](const std::string& stem, std::uint64_t ring_bytes,
                    bool step) {
    RemoveParts(stem);
    McapReadbackOptions rb;
    rb.ring_bytes = ring_bytes;
    rb.settle_bytes = 0;
    McapWriterEndpoint ep(TempPath(stem + ".mcap"), resolve,
                          /*max_bytes=*/1 << 20, /*max_duration_s=*/0.0,
                          WritePolicy::lossless(), {}, false, 0,
                          /*sync_span_bytes=*/64 * 1024, key, rb);
    ep.Start(nullptr, nullptr);
    std::atomic<bool> done{false};
    std::thread stepper;
    if (step) {
      stepper = std::thread([&] {
        while (!done.load()) ep.ReadbackStep(std::chrono::milliseconds(0));
      });
    }
    for (int i = 0; i < 2048; ++i)
      ep.Send(Data(kFirstDynamic, std::string(3000, char('a' + i % 26))));
    ep.Stop();
    done.store(true);
    if (stepper.joinable()) stepper.join();
    return ep.readback_stats();
  };
  const std::string ref = "visio_ep_rb_over_ref";
  record(ref, 0, false);
  for (const std::uint64_t ring : {64u << 10, 256u << 10, 1u << 20}) {
    SCOPED_TRACE(ring);
    const std::string on = "visio_ep_rb_over_on";
    const McapReadbackStats st = record(on, ring, true);
    EXPECT_EQ(st.spans_mismatched, 0u);
    EXPECT_EQ(st.spans_unrepaired, 0u);
    EXPECT_EQ(st.read_failed, 0u);
    EXPECT_GT(st.spans_verified + st.spans_skipped, 0u);
    ASSERT_EQ(PartCount(on), PartCount(ref));
    for (int i = 0; i < PartCount(on); ++i)
      EXPECT_EQ(DecryptPart(SlurpFile(PartPath(on, i)), key),
                DecryptPart(SlurpFile(PartPath(ref, i)), key))
          << "part " << i;
    RemoveParts(on);
  }
  RemoveParts(ref);
}


TEST(McapWriterEndpoint, EmptyQueueSleepsAndWakesForAnotherRecord) {
  for (bool continuation : {false, true}) {
    SCOPED_TRACE(continuation);
    const std::string path = TempPath("endpoint_rotation_sleep.mcap");
    auto channel = MakeChannel(16, "/imu");
    auto resolve = [&](std::uint32_t) { return &channel; };
    const auto rotation = continuation ? std::optional<McapRotationPolicy>{McapRotationPolicy{}}
                                       : std::nullopt;
    McapWriterEndpoint ep(path, resolve, 0, 0, WritePolicy::lossless(), {},
                          true, 0, 0, {}, {}, {}, {}, rotation);
    ep.Start(nullptr, nullptr);
    ep.Send(Data(16, "first"));
    for (unsigned i = 0; i < 500 && ep.bytes_written() != 5; ++i)
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    ASSERT_EQ(ep.bytes_written(), 5u);
    ExpectIdleCpu();
    ep.Send(Data(16, "second"));
    for (unsigned i = 0; i < 500 && ep.bytes_written() != 11; ++i)
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    EXPECT_EQ(ep.bytes_written(), 11u);
    ep.Stop();
    EXPECT_EQ(ep.bytes_written(), 11u);
    EXPECT_FALSE(ep.write_failed());
    std::remove(path.c_str());
  }
}

TEST(McapWriterEndpoint, IdleContinuationDeadlineFlushesWithoutAnotherSend) {
  RemoveParts("endpoint_rotation_idle");
  auto a=MakeChannel(16,"/a"), b=MakeChannel(17,"/b");
  a.schema_name=b.schema_name="foxglove.CompressedVideo";
  auto resolve=[&](std::uint32_t id) { return id==16 ? &a : &b; };
  McapRotationPolicy rotation;
  rotation.video_ids={16,17};
  rotation.max_wait=std::chrono::milliseconds(100);
  McapWriterEndpoint ep(TempPath("endpoint_rotation_idle.mcap"),resolve,10000,0,
                        WritePolicy::lossless(),{},true,0,0,{}, {}, {}, {},rotation);
  ep.Start(nullptr,nullptr);
  auto ma=Data(16,std::string(5000,'a')); ma.keyframe=true;ma.sync_point={1,1,0};
  auto mb=Data(17,std::string(5000,'b')); mb.keyframe=true;mb.sync_point={1,1,0};
  ep.Send(ma); ep.Send(mb);
  auto idr=Data(16,"next-idr"); idr.keyframe=true;idr.sync_point={1,1,30};
  ep.Send(idr);
  for (unsigned i=0;i<500;++i) {
    if (ep.stats().rotation.cancelled==1 && ep.bytes_written()==10008 && ep.pending_bytes()==0) break;
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  }
  EXPECT_EQ(ep.stats().rotation.cancelled,1u);
  EXPECT_EQ(ep.bytes_written(),10008u);
  EXPECT_EQ(ep.pending_bytes(),0u);
  ep.Stop();
  EXPECT_FALSE(ep.write_failed());
  EXPECT_EQ(PartCount("endpoint_rotation_idle"),1);
  RemoveParts("endpoint_rotation_idle");
}

namespace {
class RejectSecondPart final : public WriterStorage {
 public:
  std::unique_ptr<OutputFile> Create(const std::string& path,IoResult& result) override {
    if (++opens>1) { result={IoStep::open,ENOSPC,0,false}; return {}; }
    return LocalWriterStorage()->Create(path,result);
  }
  IoResult SyncParent(const std::string& path) override { return LocalWriterStorage()->SyncParent(path); }
  IoResult Remove(const std::string& path) override { return LocalWriterStorage()->Remove(path); }
  unsigned opens=0;
};
}
TEST(McapWriterEndpoint, ContinuationOpenFailureCanStopWithExpiredCandidate) {
  RemoveParts("endpoint_rotation_failed");
  auto a=MakeChannel(16,"/a"), b=MakeChannel(17,"/b");
  a.schema_name=b.schema_name="foxglove.CompressedVideo";
  auto resolve=[&](std::uint32_t id) { return id==16 ? &a : &b; };
  McapRotationPolicy rotation; rotation.video_ids={16,17};
  rotation.max_wait=std::chrono::milliseconds(10);
  auto storage=std::make_shared<RejectSecondPart>();
  McapWriterEndpoint ep(TempPath("endpoint_rotation_failed.mcap"),resolve,10000,0,
                        WritePolicy::lossless(),{},true,0,0,{}, {},storage,{},rotation);
  ep.Start(nullptr,nullptr);
  auto ma=Data(16,std::string(5000,'a')); ma.keyframe=true;ma.sync_point={1,1,0};
  auto mb=Data(17,std::string(5000,'b')); mb.keyframe=true;mb.sync_point={1,1,0};
  ep.Send(ma);ep.Send(mb);
  ma.payload="a-idr";mb.payload="b-idr";
  ma.sync_point.frame_index=mb.sync_point.frame_index=30;
  ep.Send(ma);ep.Send(mb);
  for (unsigned i=0;i<500 && !ep.write_failed();++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  ASSERT_TRUE(ep.write_failed());
  std::this_thread::sleep_for(std::chrono::milliseconds(30));
  ExpectIdleCpu();
  ep.Stop();
  EXPECT_EQ(ep.pending_bytes(),0u);
  EXPECT_TRUE(ep.write_failed());
  RemoveParts("endpoint_rotation_failed");
}

TEST(McapWriterEndpoint, PendingStartExpiresWithoutAnyTraffic) {
  const std::string stem="sync_endpoint_empty";RemoveParts(stem);
  auto a=MakeChannel(16,"/a");a.schema_name="foxglove.CompressedVideo";
  McapRotationPolicy policy;policy.video_ids={16};policy.start_wait=std::chrono::milliseconds(25);
  McapWriterEndpoint endpoint(TempPath(stem+".mcap"),[&](std::uint32_t){return &a;},
      1'000'000,0,WritePolicy::lossless(),{},true,0,0,{}, {}, {}, {},policy);
  const auto started=std::chrono::steady_clock::now();endpoint.Start(nullptr,nullptr);
  EXPECT_FALSE(endpoint.WaitForStart(std::chrono::milliseconds(500)));
  EXPECT_LT(std::chrono::steady_clock::now()-started,std::chrono::milliseconds(400));
  EXPECT_EQ(endpoint.start_status(),McapWriter::StartStatus::TimedOut);
  endpoint.Stop();EXPECT_EQ(PartCount(stem),0);RemoveParts(stem);
}
TEST(McapWriterEndpoint, FirstGroupUsesStartDeadlineRatherThanRotationArrivalWindow) {
  const std::string stem="endpoint_first_group_arrival";RemoveParts(stem);
  auto a=MakeChannel(16,"/head/camera/0"),b=MakeChannel(17,"/wrist/camera/0");
  a.schema_name=b.schema_name=visio_schema::kCompressedVideoSchema;
  McapRotationPolicy policy;policy.video_ids={16,17};
  policy.max_wait=std::chrono::milliseconds(20);
  policy.start_wait=std::chrono::milliseconds(500);
  McapWriterEndpoint endpoint(TempPath(stem+".mcap"),[&](std::uint32_t id) {
    return id==16?&a:&b;
  },1'000'000,0,WritePolicy::lossless(),{},true,0,0,{}, {}, {}, {},policy);
  const visio_schema::wire::SyncPoint point{42,7,0};
  endpoint.ArmStart(point);endpoint.Start(nullptr,nullptr);
  auto first=Data(16,"head-idr");first.keyframe=true;first.sync_point=point;first.timestamp.seconds=1;
  endpoint.Send(first);
  for(unsigned i=0;i<1000 && !endpoint.stats().rotation.max_staged_bytes;++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  ASSERT_GT(endpoint.stats().rotation.max_staged_bytes,0u);
  EXPECT_FALSE(endpoint.WaitForStart(std::chrono::milliseconds(60)));
  auto second=first;second.stream_id=17;second.payload="wrist-idr";
  endpoint.Send(second);
  EXPECT_TRUE(endpoint.WaitForStart(std::chrono::milliseconds(200)))
      << "transport arrival delay does not change the common capture identity or timestamp";
  endpoint.Stop();EXPECT_FALSE(endpoint.write_failed());RemoveParts(stem);
}

TEST(McapWriterEndpoint, ReconnectedCameraKeepsFrozenMembershipAndCanAlignAgain) {
  const std::string stem="endpoint_reconnected_camera";RemoveParts(stem);
  auto a=MakeChannel(16,"/head/camera/0"),b=MakeChannel(17,"/wrist/camera/0");
  a.schema_name=b.schema_name=visio_schema::kCompressedVideoSchema;
  auto reconnected=b;reconnected.id=18;
  auto newcomer=b;newcomer.id=19;newcomer.topic="/new-wrist/camera/0";
  std::unordered_map<std::uint32_t,Channel> channels{{16,a},{17,b},{18,reconnected},{19,newcomer}};
  McapRotationPolicy policy;policy.video_ids={16,17};
  McapWriterEndpoint ep(TempPath(stem+".mcap"),[&](std::uint32_t id)->const Channel* {
    const auto i=channels.find(id);return i==channels.end()?nullptr:&i->second;
  },1000,0,WritePolicy::lossless(),{},true,0,0,{}, {}, {}, {},policy);
  auto idr=[](std::uint32_t id,std::string data,std::uint64_t frame) {
    auto m=Data(id,std::move(data));m.keyframe=true;m.sync_point={42,7,frame};
    m.timestamp.seconds=frame/30;return m;
  };
  ep.Start(nullptr,nullptr);
  ep.Send(idr(16,"first-head",30));ep.Send(idr(17,"first-wrist",30));
  ASSERT_TRUE(ep.WaitForStart(std::chrono::milliseconds(500)));
  ep.Send(Data(16,std::string(990,'p')));
  ep.Send(idr(16,"next-head",60));ep.Send(idr(18,"after-reconnect",60));
  ep.Send(idr(19,"newcomer-must-wait",60));
  ep.Stop();EXPECT_FALSE(ep.write_failed());ASSERT_EQ(PartCount(stem),2);
  const auto tail=SlurpFile(PartPath(stem,1));
  EXPECT_NE(tail.find("after-reconnect"),std::string::npos);
  EXPECT_EQ(tail.find("newcomer-must-wait"),std::string::npos);
  RemoveParts(stem);
}

TEST(McapWriterEndpoint, UnselectedVideoCannotEvictSelectedRecordingData) {
  const std::string stem = "endpoint_unselected_admission";
  RemoveParts(stem);
  auto selected = MakeChannel(16, "/head/camera/0");
  auto newcomer = MakeChannel(17, "/new-wrist/camera/0");
  selected.schema_name = newcomer.schema_name = visio_schema::kCompressedVideoSchema;
  McapRotationPolicy rotation;
  rotation.video_ids = {16};
  auto policy = WritePolicy::stale_eviction(128, std::chrono::microseconds(0));
  McapWriterEndpoint endpoint(TempPath(stem + ".mcap"), [&](std::uint32_t id) {
    return id == 16 ? &selected : &newcomer;
  }, 1'000'000, 0, policy, {}, true, 0, 0, {}, {}, {}, {}, rotation);
  endpoint.Send(Data(16, std::string(32, 'a')));
  for (int i = 0; i < 4; ++i)
    endpoint.Send(Data(17, std::string(64, 'x')));
  EXPECT_EQ(endpoint.pending_frames(), 1u);
  EXPECT_EQ(endpoint.pending_bytes(), 32u);
  EXPECT_EQ(endpoint.stats().dropped, 0u);
  endpoint.Stop();
  RemoveParts(stem);
}
