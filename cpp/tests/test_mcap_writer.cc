// McapWriter tests — writes a non-empty MCAP from (Channel, Message) pairs and
// rotates into numbered parts. Behavioural level; full MCAP content + Foxglove
// readability checks live in the Python suite (test_recording.py,
// test_mcap_foxglove_e2e.py).
#include "visio_schema/mcap/writer.hpp"

#include <gtest/gtest.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>
#include <vector>

#if defined(__linux__)
#include <climits>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#endif

#include "visio_schema/routing/channel.hpp"
#include "visio_schema/wire/control.hpp"
#include "visio_schema/wire/time.hpp"   // SetTimestampNs

using visio_schema::Channel;
using visio_schema::kFirstDynamic;
using visio_schema::mcap::McapWriter;
using visio_schema::SetTimestampNs;
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

// A compressed-video channel — the only schema the keyframe gate acts on.
Channel VideoChannel(std::uint32_t id, const std::string& topic) {
  Channel c;
  c.id = id;
  c.topic = topic;
  c.schema_name = "foxglove.CompressedVideo";
  c.schema = std::string(8, '\x01');
  return c;
}

// A channel with a given non-video schema (audio, imu, …) to prove it is never
// gated even when carrying no keyframe flag.
Channel NonVideoChannel(std::uint32_t id, const std::string& topic,
                        const std::string& schema) {
  Channel c;
  c.id = id;
  c.topic = topic;
  c.schema_name = schema;
  c.schema = std::string(8, '\x01');
  return c;
}

// A video frame with an explicit keyframe flag and capture timestamp. The `ts_ns`
// carries pair identity for rotate-on-keyframe: a co-phased pair shares one ts.
Message VideoMsg(std::uint32_t id, std::string payload, bool keyframe,
                 std::int64_t ts_ns) {
  Message m;
  m.stream_id = id;
  m.payload = std::move(payload);
  m.bulk = true;
  m.keyframe = keyframe;
  SetTimestampNs(&m.timestamp, ts_ns);
  return m;
}

std::string PartPath(const std::string& stem_no_ext, int part) {
  char buf[8];
  std::snprintf(buf, sizeof(buf), "_%04d", part);
  return TempPath(stem_no_ext + buf + ".mcap");
}

std::string SlurpFile(const std::string& path) {
  std::ifstream f(path, std::ios::binary);
  return std::string((std::istreambuf_iterator<char>(f)),
                     std::istreambuf_iterator<char>());
}

void RemoveParts(const std::string& stem_no_ext) {
  for (int i = 0; i < 8; ++i) std::remove(PartPath(stem_no_ext, i).c_str());
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

TEST(McapWriter, SyncSpansNeverAlterTheBytes) {
  // sync_span_bytes changes WHEN pages reach storage, never WHAT is
  // written. Messages reach the file as ~768 KiB mcap chunk blobs, so
  // ~3 MB of payload drives several SyncSpan() passes — including the
  // wait-and-evict of the PREVIOUS span — and the output must stay
  // byte-identical to the unsynced writer's.
  const std::string plain = TempPath("visio_schema_mcap_nosync.mcap");
  const std::string synced = TempPath("visio_schema_mcap_sync.mcap");
  std::remove(plain.c_str());
  std::remove(synced.c_str());
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  const std::string payload(3000, 'p');
  auto write_all = [&](McapWriter& w) {
    for (int i = 0; i < 1024; ++i) w.Write(ch, Data(kFirstDynamic, payload));
    w.Close();
  };
  {
    McapWriter w(plain);
    write_all(w);
  }
  {
    // Byte-identity alone cannot tell a working sync from one that fails
    // every call; the one-shot failure log can. Its absence is the
    // liveness half of this pin (syscall effects themselves are verified
    // on-device via /proc/meminfo Dirty during a recording).
    testing::internal::CaptureStderr();
    McapWriter w(synced, 0, 0.0, false, 0, /*sync_span_bytes=*/4096);
    write_all(w);
    const std::string logged = testing::internal::GetCapturedStderr();
    EXPECT_EQ(logged.find("SyncSpan failed"), std::string::npos) << logged;
    EXPECT_EQ(logged.find("span writeback failed"), std::string::npos)
        << logged;
  }
  const std::string ba = SlurpFile(plain);
  const std::string bb = SlurpFile(synced);
  EXPECT_GT(bb.size(), 2u * 1024u * 1024u);  // several spans actually ran
  ASSERT_EQ(ba.size(), bb.size());
  // Safety pin only: byte-identity holds whether or not the syscalls fire
  // (their liveness is verified on-device via /proc/meminfo Dirty during a
  // recording). Compare without gtest's multi-MB operand dump on failure.
  const auto mis = std::mismatch(ba.begin(), ba.end(), bb.begin());
  EXPECT_TRUE(mis.first == ba.end())
      << "first mismatch at byte " << (mis.first - ba.begin());
  std::remove(plain.c_str());
  std::remove(synced.c_str());
}

TEST(McapWriter, SyncSpansSurviveRotationByteIdentical) {
  // Every rotated part gets its own writable, so the span knob must reach
  // part 1, 2, ... — not just part 0. Byte-identity per part pins that.
  const std::string stem_a = "visio_schema_mcap_rot_nosync";
  const std::string stem_b = "visio_schema_mcap_rot_sync";
  RemoveParts(stem_a);
  RemoveParts(stem_b);
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  const std::string payload(3000, 'p');
  auto write_all = [&](McapWriter& w) {
    for (int i = 0; i < 1024; ++i) w.Write(ch, Data(kFirstDynamic, payload));
    w.Close();
  };
  {
    McapWriter w(TempPath(stem_a + ".mcap"), /*max_bytes=*/1024 * 1024);
    write_all(w);
  }
  {
    McapWriter w(TempPath(stem_b + ".mcap"), /*max_bytes=*/1024 * 1024, 0.0,
                 false, 0, /*sync_span_bytes=*/4096);
    write_all(w);
  }
  for (int part = 0; part < 3; ++part) {
    const std::string pa = PartPath(stem_a, part);
    const std::string pb = PartPath(stem_b, part);
    ASSERT_TRUE(fs::exists(pa)) << pa;
    ASSERT_TRUE(fs::exists(pb)) << pb;
    EXPECT_TRUE(SlurpFile(pa) == SlurpFile(pb)) << "part " << part;
  }
  RemoveParts(stem_a);
  RemoveParts(stem_b);
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

// ---- Keyframe gate (always on) + rotate-at-keyframe (opt-in) ---------------
//
// bytes_written() only advances on an ACTUAL write, so dropped (gated) frames
// are invisible to it — with distinct payload sizes the total uniquely
// fingerprints exactly which frames were kept, per channel.

// (a) A video stream's leading P-frames are dropped until its first keyframe.
TEST(McapWriterGate, DropsLeadingPFramesUntilFirstKeyframe) {
  const std::string path = TempPath("visio_schema_gate_a.mcap");
  std::remove(path.c_str());
  const Channel v = VideoChannel(kFirstDynamic, "/dev/camera/0");
  McapWriter w(path);
  w.Write(v, VideoMsg(kFirstDynamic, std::string(3, 'a'), /*kf=*/false, 1000));
  EXPECT_EQ(w.bytes_written(), 0u);   // pre-keyframe P-frame dropped
  w.Write(v, VideoMsg(kFirstDynamic, std::string(9, 'a'), /*kf=*/true, 2000));
  EXPECT_EQ(w.bytes_written(), 9u);   // first IDR primes + writes
  w.Write(v, VideoMsg(kFirstDynamic, std::string(5, 'a'), /*kf=*/false, 3000));
  EXPECT_EQ(w.bytes_written(), 14u);  // subsequent P-frames flow
  w.Close();
  std::remove(path.c_str());
}

// (b) Non-video channels (audio, imu) are never gated, keyframe flag or not.
TEST(McapWriterGate, AudioAndImuNeverGated) {
  const std::string path = TempPath("visio_schema_gate_b.mcap");
  std::remove(path.c_str());
  const Channel imu = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  const Channel audio =
      NonVideoChannel(kFirstDynamic + 1, "/dev/audio/0", "foxglove.RawAudio");
  McapWriter w(path);
  w.Write(imu, Data(kFirstDynamic, "abcd"));            // 4, written immediately
  EXPECT_EQ(w.bytes_written(), 4u);
  w.Write(audio, Data(kFirstDynamic + 1, "audiodata"));  // 9, RawAudio + no keyframe
  EXPECT_EQ(w.bytes_written(), 13u);                     // not gated
  w.Close();
  std::remove(path.c_str());
}

// (c) After a plain byte-exact roll, the new part re-drops video until ITS first
// keyframe (the gate runs regardless of rotate_on_keyframe).
TEST(McapWriterGate, RotationReDropsUntilKeyframe) {
  RemoveParts("visio_schema_gate_c");
  const std::string path = TempPath("visio_schema_gate_c.mcap");
  const Channel v = VideoChannel(kFirstDynamic, "/dev/camera/0");
  {
    McapWriter w(path, /*max_bytes=*/16);  // rotate_on_keyframe defaults off
    w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), true, 1000));   // part0 prime
    w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), false, 2000));  // part0, pb=20
    w.Write(v, VideoMsg(kFirstDynamic, std::string(5, 'a'), false, 3000));   // roll → part1, drop
    EXPECT_EQ(w.bytes_written(), 20u);
    w.Write(v, VideoMsg(kFirstDynamic, std::string(7, 'a'), true, 4000));    // part1 prime
    EXPECT_EQ(w.bytes_written(), 27u);
    w.Close();
  }
  EXPECT_TRUE(fs::exists(TempPath("visio_schema_gate_c_0001.mcap")));
  RemoveParts("visio_schema_gate_c");
}

// (d) Two video channels are gated independently (each opens on its own IDR).
TEST(McapWriterGate, TwoVideoChannelsGatedIndependently) {
  const std::string path = TempPath("visio_schema_gate_d.mcap");
  std::remove(path.c_str());
  const Channel v0 = VideoChannel(kFirstDynamic, "/dev/camera/0");
  const Channel v1 = VideoChannel(kFirstDynamic + 1, "/dev/camera/1");
  McapWriter w(path);
  w.Write(v0, VideoMsg(kFirstDynamic, std::string(10, 'a'), true, 1000));      // v0 prime
  w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(5, 'b'), false, 1000));  // v1 drop
  w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(5, 'b'), false, 2000));  // v1 drop
  w.Write(v0, VideoMsg(kFirstDynamic, std::string(3, 'a'), false, 2000));      // v0 write
  w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(7, 'b'), true, 3000));   // v1 prime
  EXPECT_EQ(w.bytes_written(), 20u);  // 10 + 3 + 7 (v1's two P-frames dropped)
  w.Close();
  std::remove(path.c_str());
}

// (e) A part that never gets a keyframe stays empty and must NOT spuriously roll
// (ShouldRoll never fires while part_bytes_ == 0).
TEST(McapWriterGate, PartWithNoKeyframeStaysEmptyAndDoesNotRoll) {
  RemoveParts("visio_schema_gate_e");
  const std::string path = TempPath("visio_schema_gate_e.mcap");
  const Channel v = VideoChannel(kFirstDynamic, "/dev/camera/0");
  {
    McapWriter w(path, /*max_bytes=*/16);
    for (int i = 0; i < 10; ++i)
      w.Write(v, VideoMsg(kFirstDynamic, std::string(100, 'a'), false, 1000 + i));
    EXPECT_EQ(w.bytes_written(), 0u);
    w.Close();
  }
  EXPECT_TRUE(fs::exists(TempPath("visio_schema_gate_e_0000.mcap")));
  EXPECT_FALSE(fs::exists(TempPath("visio_schema_gate_e_0001.mcap")));  // never rolled
  RemoveParts("visio_schema_gate_e");
}

// (f) rotate_on_keyframe: once the cap is crossed mid-GOP, the part keeps growing
// (no roll on P-frames) until a NEW-pair keyframe arrives, which cuts cleanly.
TEST(McapWriterRotateKeyframe, DefersRollToNextKeyframe) {
  RemoveParts("visio_schema_gate_f");
  const std::string path = TempPath("visio_schema_gate_f.mcap");
  const std::string p1 = TempPath("visio_schema_gate_f_0001.mcap");
  const Channel v = VideoChannel(kFirstDynamic, "/dev/camera/0");
  {
    McapWriter w(path, /*max_bytes=*/50, /*max_duration_s=*/0.0,
                 /*rotate_on_keyframe=*/true, /*pair_guard_ns=*/500);
    w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), true, 1000));   // prime, pb=10
    for (int i = 0; i < 4; ++i)                                              // pb -> 50
      w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), false, 2000 + i));
    EXPECT_EQ(w.bytes_written(), 50u);
    EXPECT_FALSE(fs::exists(p1));  // full, but no keyframe yet → no roll
    w.Write(v, VideoMsg(kFirstDynamic, std::string(7, 'a'), true, 1'000'000));  // new pair → roll
    EXPECT_EQ(w.bytes_written(), 57u);
    EXPECT_TRUE(fs::exists(p1));   // rolled, new part opens on the keyframe
    w.Close();
  }
  RemoveParts("visio_schema_gate_f");
}

// (g) rotate_on_keyframe hard ceiling: if keyframes stall, the part still rolls at
// max_bytes + 1/8 rather than growing unbounded (then the gate drops the tail).
TEST(McapWriterRotateKeyframe, HardCeilingRollsWhenKeyframesStall) {
  RemoveParts("visio_schema_gate_g");
  const std::string path = TempPath("visio_schema_gate_g.mcap");
  const std::string p1 = TempPath("visio_schema_gate_g_0001.mcap");
  const Channel v = VideoChannel(kFirstDynamic, "/dev/camera/0");
  {
    McapWriter w(path, /*max_bytes=*/50, 0.0, /*rotate_on_keyframe=*/true, 500);
    w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), true, 1000));   // prime
    for (int i = 0; i < 10; ++i)                                             // only P-frames
      w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), false, 2000 + i));
    // p0 grew to the ceiling (50 + 50/8 = 56) then rolled; the post-roll P-frames
    // drop (new part unprimed, no keyframe ever). Only p0's content is written.
    EXPECT_EQ(w.bytes_written(), 60u);            // 10 + 5×10 into p0 before the ceiling roll
    EXPECT_TRUE(fs::exists(p1));                  // ceiling forced a roll despite no keyframe
    w.Close();
  }
  RemoveParts("visio_schema_gate_g");
}

// (h) opt-in OFF preserves the original byte-exact roll: a mid-stream P-frame
// triggers the rotation (contrast with (f), where it waits for a keyframe).
TEST(McapWriterRotateKeyframe, OptInOffRollsByteExactMidStream) {
  RemoveParts("visio_schema_gate_h");
  const std::string path = TempPath("visio_schema_gate_h.mcap");
  const std::string p1 = TempPath("visio_schema_gate_h_0001.mcap");
  const Channel v = VideoChannel(kFirstDynamic, "/dev/camera/0");
  {
    McapWriter w(path, /*max_bytes=*/16);  // rotate_on_keyframe off
    w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), true, 1000));   // prime, pb=10
    w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), false, 2000));  // pb=20
    EXPECT_FALSE(fs::exists(p1));
    w.Write(v, VideoMsg(kFirstDynamic, std::string(5, 'a'), false, 3000));   // rolls on a P-frame
    EXPECT_TRUE(fs::exists(p1));            // byte-exact roll fired mid-stream
    w.Close();
  }
  RemoveParts("visio_schema_gate_h");
}

// (i) Documented degradation: with rotate_on_keyframe but IDRs NOT co-phased, the
// eye whose keyframe doesn't trigger the roll loses frames until its own next IDR.
TEST(McapWriterRotateKeyframe, UnsyncedMultiCamDropsTheNonTriggeringEye) {
  RemoveParts("visio_schema_gate_i");
  const std::string path = TempPath("visio_schema_gate_i.mcap");
  const Channel v0 = VideoChannel(kFirstDynamic, "/dev/camera/0");
  const Channel v1 = VideoChannel(kFirstDynamic + 1, "/dev/camera/1");
  {
    McapWriter w(path, /*max_bytes=*/50, 0.0, /*rotate_on_keyframe=*/true, 500);
    w.Write(v0, VideoMsg(kFirstDynamic, std::string(10, 'a'), true, 1000));      // both prime
    w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(10, 'b'), true, 1000));
    w.Write(v0, VideoMsg(kFirstDynamic, std::string(10, 'a'), false, 2000));
    w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(10, 'b'), false, 2000));
    w.Write(v0, VideoMsg(kFirstDynamic, std::string(10, 'a'), false, 3000));     // pb=50
    // v0's keyframe (a new pair) triggers the roll — v1 has NO keyframe here.
    w.Write(v0, VideoMsg(kFirstDynamic, std::string(8, 'a'), true, 1'000'000));  // roll, v0 primed
    w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(5, 'b'), false, 1'000'000));  // v1 DROPPED
    w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(5, 'b'), false, 1'050'000));  // v1 DROPPED
    w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(6, 'b'), true, 2'000'000));   // v1's own IDR
    EXPECT_EQ(w.bytes_written(), 64u);  // 50 + 8 + 6; v1 lost 10 bytes (its offset gap)
    w.Close();
  }
  RemoveParts("visio_schema_gate_i");
}

// (j) THE regression this design exists for: the byte cap is crossed WHILE writing
// eye0's keyframe of pair M; eye1's keyframe of the SAME pair (same ts) must NOT
// trigger a roll — the pair stays whole in one part, and the cut lands at M+GOP.
TEST(McapWriterRotateKeyframe, DoesNotSplitACoPhasedPair) {
  RemoveParts("visio_schema_gate_j");
  const std::string path = TempPath("visio_schema_gate_j.mcap");
  const std::string p1 = TempPath("visio_schema_gate_j_0001.mcap");
  const Channel v0 = VideoChannel(kFirstDynamic, "/dev/camera/0");
  const Channel v1 = VideoChannel(kFirstDynamic + 1, "/dev/camera/1");
  {
    McapWriter w(path, /*max_bytes=*/50, 0.0, /*rotate_on_keyframe=*/true, 500);
    // An earlier pair fills part0 to 30.
    w.Write(v0, VideoMsg(kFirstDynamic, std::string(15, 'a'), true, 10'000));
    w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(15, 'b'), true, 10'000));
    // Pair M: eye0's keyframe pushes part0 to exactly the cap (50)...
    w.Write(v0, VideoMsg(kFirstDynamic, std::string(20, 'a'), true, 100'000));
    EXPECT_FALSE(fs::exists(p1));
    // ...then eye1's keyframe of the SAME pair (ts == part_max) must land in the
    // SAME part — never trigger a roll on the sibling (the split bug).
    w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(20, 'b'), true, 100'000));
    EXPECT_FALSE(fs::exists(p1)) << "co-phased pair M was split across the rotation";
    EXPECT_EQ(w.bytes_written(), 70u);  // 15+15+20+20, pair M whole in part0
    // The NEXT pair (M+GOP) is where the deferred cut lands.
    w.Write(v0, VideoMsg(kFirstDynamic, std::string(10, 'a'), true, 200'000));   // roll → part1
    w.Write(v1, VideoMsg(kFirstDynamic + 1, std::string(10, 'b'), true, 200'000));
    EXPECT_TRUE(fs::exists(p1));          // clean cut at M+GOP, both IDRs in part1
    EXPECT_EQ(w.bytes_written(), 90u);
    w.Close();
  }
  RemoveParts("visio_schema_gate_j");
}

// noChunkCRC: chunk records must still be written (chunking carries the
// per-chunk message index readers seek by) but with uncompressed_crc == 0 —
// the spec's "not computed", which every reader skips. Pins BOTH halves of
// the writer option: flipping noChunking too would silently drop the seek
// index, and re-enabling the CRC would put the per-byte checksum back on the
// device's hot path. Raw little-endian record scan — no C++ MCAP reader
// exists in this repo, and none is needed for a field this shallow.
TEST(McapWriter, WritesChunksWithZeroCrc) {
  const std::string path = TempPath("visio_schema_mcap_nocrc.mcap");
  std::remove(path.c_str());
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  {
    McapWriter w(path);
    w.Write(ch, Data(kFirstDynamic, "frame-0"));
    w.Close();
  }

  std::ifstream f(path, std::ios::binary);
  const std::vector<unsigned char> b{std::istreambuf_iterator<char>(f), {}};
  ASSERT_GT(b.size(), 8u);
  int chunks = 0;
  for (std::size_t off = 8; off + 9 <= b.size();) {  // skip leading magic
    std::uint64_t len = 0;
    std::memcpy(&len, &b[off + 1], 8);
    if (off + 9 + len > b.size()) break;             // trailing magic
    if (b[off] == 0x06 && len >= 28) {               // Chunk record
      ++chunks;
      // Body: start time u64, end time u64, uncompressed_size u64, then crc.
      std::uint32_t crc = 0;
      std::memcpy(&crc, &b[off + 9 + 24], 4);
      EXPECT_EQ(crc, 0u);
    }
    off += 9 + len;
  }
  EXPECT_GE(chunks, 1) << "chunking must stay ON (seek index)";
  std::remove(path.c_str());
}

// The part file rides a 256 KiB stdio buffer (CloexecFileWriter::open). Cross
// that buffer several times AND rotate mid-stream, then prove every part was
// fully flushed: each must end with the MCAP trailing magic — a truncated
// buffered tail would leave a torn footer that read_mcap rejects. The tiny
// writes in the other tests never fill the buffer even once.
TEST(McapWriter, LargeBufferedWritesFlushAcrossCloseAndRotation) {
  const std::string stem = "visio_schema_mcap_bigbuf";
  const std::string path = TempPath(stem + ".mcap");
  RemoveParts(stem);
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  {
    McapWriter w(path, /*max_bytes=*/400 * 1024);
    // ~1 MB total -> at least three parts, each crossing the buffer.
    for (int i = 0; i < 64; ++i) {
      w.Write(ch, Data(kFirstDynamic, std::string(16 * 1024, char('a' + i % 26))));
    }
    w.Close();
  }
  static const char kMagic[] = "\x89MCAP0\r\n";
  int parts = 0;
  for (int i = 0; i < 8; ++i) {
    char buf[8];
    std::snprintf(buf, sizeof(buf), "_%04d", i);
    const std::string p = TempPath(stem + buf + ".mcap");
    if (!fs::exists(p)) break;
    ++parts;
    std::ifstream f(p, std::ios::binary);
    ASSERT_TRUE(f.good()) << p;
    f.seekg(-8, std::ios::end);
    char tail[8] = {0};
    f.read(tail, 8);
    EXPECT_EQ(std::memcmp(tail, kMagic, 8), 0)
        << p << " lacks the trailing magic — buffered tail lost";
  }
  EXPECT_GE(parts, 3) << "expected rotation across the stdio buffer";
  RemoveParts(stem);
}
