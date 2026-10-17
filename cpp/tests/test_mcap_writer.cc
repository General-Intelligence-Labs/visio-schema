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

#define MCAP_COMPRESSION_NO_LZ4
#define MCAP_COMPRESSION_NO_ZSTD
#define MCAP_IMPLEMENTATION
// Upstream types.inl emits non-inline symbols in both writer and reader TUs.
// Isolate the independent test reader to avoid an ODR clash with the writer.
#define mcap verification_mcap
#include <mcap/reader.hpp>
#undef mcap
#undef MCAP_IMPLEMENTATION
#include <thread>
#include <sstream>
#include "mcap_writer_test_util.hpp"
#include "visio_schema/routing/channel.hpp"
#include "visio_schema/wire/control.hpp"
#include "visio_schema/wire/time.hpp"   // SetTimestampNs

using namespace mcap_test;
using visio_schema::Channel;
using visio_schema::kFirstDynamic;
using visio_schema::mcap::McapWriter;
using visio_schema::SetTimestampNs;
using visio_schema::wire::Message;
namespace fs = std::filesystem;

namespace {

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
                 std::int64_t ts_ns, std::uint32_t sequence = 0) {
  Message m;
  m.stream_id = id;
  m.seq = sequence;
  m.payload = std::move(payload);
  m.bulk = true;
  m.keyframe = keyframe;
  SetTimestampNs(&m.timestamp, ts_ns);
  return m;
}

// Read each closed part through the independent MCAP reader, checking the
// exact record stream rather than only the writer's byte counters.
std::vector<std::string> ContinuationRows(const std::string& stem,
                                        std::optional<visio_schema::mcap::RecordingKey> key = {}) {
  std::vector<std::string> rows;
  std::string identity;
  for (int part = 0; part < PartCount(stem); ++part) {
    auto raw = SlurpFile(PartPath(stem, part));
    if (key) raw = DecryptPart(raw, *key);
    EXPECT_TRUE(EndsWithMcapMagic(raw));
    ::verification_mcap::BufferReader input;
    input.reset(reinterpret_cast<const std::byte*>(raw.data()), raw.size(), raw.size());
    ::verification_mcap::McapReader reader;
    EXPECT_TRUE(reader.open(input).ok());
    ::verification_mcap::TypedRecordReader records(input, 8);
    int metadata_count = 0;
    records.onMetadata = [&](const auto& metadata, auto) {
      if (metadata.name != "visio.rotation") return;
      ++metadata_count;
      EXPECT_EQ(metadata.metadata.at("part_index"),std::to_string(part));
      EXPECT_EQ(metadata.metadata.at("video_continuation"),"true");
      if (identity.empty()) identity = metadata.metadata.at("recording_id");
      EXPECT_EQ(metadata.metadata.at("recording_id"),identity);
      EXPECT_FALSE(identity.empty());
      if (part == 0) EXPECT_EQ(metadata.metadata.at("cut_reason"),"initial");
    };
    while (records.next()) {}
    EXPECT_TRUE(records.status().ok());
    EXPECT_EQ(metadata_count,1);
    for (const auto& row : reader.readMessages()) {
      rows.emplace_back(row.channel->topic + ":" + std::to_string(row.message.sequence) + ":" + std::to_string(row.message.logTime) + ":" +
                        std::string(reinterpret_cast<const char*>(row.message.data), row.message.dataSize));
    }
  }
  return rows;
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
  ASSERT_GE(PartCount(stem_a), 3);
  ASSERT_EQ(PartCount(stem_b), PartCount(stem_a));
  for (int part = 0; part < PartCount(stem_a); ++part) {
    EXPECT_TRUE(SlurpFile(PartPath(stem_a, part)) ==
                SlurpFile(PartPath(stem_b, part)))
        << "part " << part;
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

// (e2) The mirror of (e) one part along: a part that WAS rolled into and then
// never received a message is removed at close. Such a part carries nothing by
// construction, and when the write path is what failed it is left without even
// a header — a file no reader can open, sitting in an otherwise valid session.
// Part 0 is exempt (see (e) above: an empty part 0 is kept, because removing it
// would leave a session with no recording at all).
TEST(McapWriterGate, RotatedPartThatNeverGotAMessageIsRemoved) {
  RemoveParts("visio_schema_gate_e2");
  const std::string path = TempPath("visio_schema_gate_e2.mcap");
  const Channel v = VideoChannel(kFirstDynamic, "/dev/camera/0");
  {
    McapWriter w(path, /*max_bytes=*/16);  // rotate_on_keyframe defaults off
    w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), true, 1000));   // part0 prime
    w.Write(v, VideoMsg(kFirstDynamic, std::string(10, 'a'), false, 2000));  // part0, pb=20
    // Rolls into part1, where the keyframe gate then drops this P-frame: part1
    // exists on disk and holds nothing. Closing here is the window the fix is
    // about — no IDR ever arrives to prime it.
    w.Write(v, VideoMsg(kFirstDynamic, std::string(5, 'a'), false, 3000));
    EXPECT_EQ(w.bytes_written(), 20u);
    w.Close();
  }
  EXPECT_TRUE(fs::exists(TempPath("visio_schema_gate_e2_0000.mcap")));
  EXPECT_FALSE(fs::exists(TempPath("visio_schema_gate_e2_0001.mcap")))
      << "an empty rolled part must not be left on the card";
  RemoveParts("visio_schema_gate_e2");
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

// Bitwise reflected CRC-32 (poly 0xEDB88320). Deliberately shares no table
// with the library's slicing-by-8, so the two cannot be wrong together.
std::uint32_t ReferenceCrc32(const unsigned char* p, std::size_t n) {
  std::uint32_t c = 0xFFFFFFFFu;
  for (std::size_t i = 0; i < n; ++i) {
    c ^= p[i];
    for (int k = 0; k < 8; ++k) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
  }
  return ~c;
}

// Every chunk carries the CRC of its uncompressed records, and chunking stays
// on (it carries the per-chunk message index readers seek by). Raw
// little-endian record scan — no C++
// MCAP reader exists in this repo, and none is needed for a field this
// shallow.
TEST(McapWriter, WritesChunksWithVerifiedCrc) {
  const std::string path = TempPath("visio_schema_mcap_crc.mcap");
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
    if (b[off] == 0x06 && len >= 40) {               // Chunk record
      ++chunks;
      // Body: start u64, end u64, uncompressed_size u64, crc u32,
      // compression (u32 length + bytes), records_len u64, records.
      const unsigned char* body = &b[off + 9];
      std::uint64_t usize = 0, rlen = 0;
      std::uint32_t crc = 0, clen = 0;
      std::memcpy(&usize, body + 16, 8);
      std::memcpy(&crc, body + 24, 4);
      std::memcpy(&clen, body + 28, 4);
      ASSERT_EQ(clen, 0u) << "chunks are uncompressed";
      std::memcpy(&rlen, body + 32, 8);
      ASSERT_EQ(rlen, usize);
      const unsigned char* records = &b[off + 9 + 40];
      EXPECT_NE(crc, 0u);
      EXPECT_EQ(crc, ReferenceCrc32(records, rlen));
    }
    off += 9 + len;
  }
  EXPECT_GE(chunks, 1) << "chunking must stay ON (seek index)";
  std::remove(path.c_str());
}

// The part file rides a small stdio buffer (kStdioBufferBytes,
// CloexecFileWriter::open — 16 KiB, sized to the only writes that ever reach
// it). Cross it several times AND rotate mid-stream, then prove every part was
// fully flushed: each must end with the MCAP trailing magic — a truncated
// buffered tail would leave a torn footer that read_mcap rejects.
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
  const int parts = PartCount(stem);
  for (int i = 0; i < parts; ++i) {
    EXPECT_TRUE(EndsWithMcapMagic(SlurpFile(PartPath(stem, i))))
        << PartPath(stem, i)
        << " lacks the trailing magic — buffered tail lost";
  }
  EXPECT_GE(parts, 3) << "expected rotation across the stdio buffer";
  RemoveParts(stem);
}

// ─────────────────────────────────────────────────────────────────────────
// VREC: the same writer, same filename, same rotation — different bytes.
//
// The load-bearing property is the OFFSET IDENTITY: decrypting from byte 32
// must reproduce, byte for byte, the plaintext MCAP the very same writes
// produce without a key. If that holds, every MCAP index offset stays valid
// and a seeking reader keeps working. These compare the two directly rather
// than asserting properties of the ciphertext in isolation.
// ─────────────────────────────────────────────────────────────────────────
namespace {

using visio_schema::mcap::kVrecHeaderBytes;
using visio_schema::mcap::ParseVrecHeader;
using visio_schema::mcap::RecordingKey;
using visio_schema::mcap::RecordingKeyFingerprint;
using visio_schema::mcap::VrecHeader;

// Identical writes on both sides of every comparison below, so any difference
// in the output is attributable to encryption alone.
void WriteFixture(McapWriter& w) {
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  for (int i = 0; i < 64; ++i)
    w.Write(ch, Data(kFirstDynamic, "frame-" + std::to_string(i)));
  w.Close();
}

}  // namespace

TEST(McapWriterVrec, DecryptingFromByte32ReproducesThePlaintextFileExactly) {
  const std::string plain = TempPath("visio_schema_vrec_plain.mcap");
  const std::string enc = TempPath("visio_schema_vrec_enc.mcap");
  std::remove(plain.c_str());
  std::remove(enc.c_str());
  const RecordingKey key = TestKey(7);

  {
    McapWriter w(plain);
    WriteFixture(w);
  }
  {
    McapWriter w(enc, 0, 0.0, false, 0, 0, key);
    WriteFixture(w);
  }

  const std::string want = SlurpFile(plain);
  const std::string got = SlurpFile(enc);
  ASSERT_FALSE(want.empty());
  // Exactly the header's worth of growth — this IS the offset identity.
  ASSERT_EQ(got.size(), want.size() + kVrecHeaderBytes);

  VrecHeader header;
  std::string err;
  ASSERT_TRUE(ParseVrecHeader(reinterpret_cast<const std::uint8_t*>(got.data()),
                              got.size(), &header, &err))
      << err;
  EXPECT_EQ(header.key_fp, RecordingKeyFingerprint(key));
  EXPECT_EQ(DecryptPart(got, key), want);

  std::remove(plain.c_str());
  std::remove(enc.c_str());
}

// The fixture above never crosses one cipher staging buffer, so it never enters
// handleWrite's slicing loop — the loop whose bound and destination both moved
// when the scratch became a heap buffer sized against the stdio one. Drive
// megabytes through it, across a rotation, and prove every part still decrypts
// to exactly the plaintext writer's bytes.
TEST(McapWriterVrec, LargeSpansSliceThroughTheScratchAndDecryptExactly) {
  const std::string stem_plain = "visio_schema_vrec_big_plain";
  const std::string stem_enc = "visio_schema_vrec_big_enc";
  RemoveParts(stem_plain);
  RemoveParts(stem_enc);
  const RecordingKey key = TestKey(11);
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  // 16 KiB records: each mcap chunk blob is ~768 KiB, i.e. a dozen slices
  // through the 64 KiB scratch, and 1 MiB parts force several rotations (every
  // one of which must re-arm the scratch alongside its own cipher).
  const std::string payload(16 * 1024, 'q');
  auto write_all = [&](McapWriter& w) {
    for (int i = 0; i < 192; ++i) w.Write(ch, Data(kFirstDynamic, payload));
    w.Close();
  };
  {
    McapWriter w(TempPath(stem_plain + ".mcap"), /*max_bytes=*/1024 * 1024);
    write_all(w);
  }
  {
    McapWriter w(TempPath(stem_enc + ".mcap"), /*max_bytes=*/1024 * 1024, 0.0,
                 false, 0, 0, key);
    write_all(w);
  }
  ASSERT_GE(PartCount(stem_plain), 2);
  ASSERT_EQ(PartCount(stem_enc), PartCount(stem_plain));
  for (int part = 0; part < PartCount(stem_plain); ++part) {
    const std::string want = SlurpFile(PartPath(stem_plain, part));
    const std::string got = SlurpFile(PartPath(stem_enc, part));
    ASSERT_GT(want.size(), 64u * 1024u) << "part " << part << " never sliced";
    EXPECT_EQ(got.size(), want.size() + kVrecHeaderBytes) << "part " << part;
    EXPECT_TRUE(DecryptPart(got, key) == want) << "part " << part;
  }
  RemoveParts(stem_plain);
  RemoveParts(stem_enc);
}

TEST(McapWriterVrec, TheCiphertextDoesNotLeakThePlaintextMagic) {
  // The cheapest regression guard on "is it actually encrypted": a plaintext
  // MCAP opens with \x89MCAP and repeats the magic at its footer, so a writer
  // that silently skipped the cipher would still contain it.
  const std::string enc = TempPath("visio_schema_vrec_magic.mcap");
  std::remove(enc.c_str());
  {
    McapWriter w(enc, 0, 0.0, false, 0, 0, TestKey(11));
    WriteFixture(w);
  }
  const std::string got = SlurpFile(enc);
  EXPECT_EQ(got.compare(0, 4, "VREC"), 0);
  EXPECT_EQ(got.find("\x89MCAP", kVrecHeaderBytes), std::string::npos);
  std::remove(enc.c_str());
}

TEST(McapWriterVrec, EachPartGetsItsOwnNonceSoRotationNeverReusesAKeystream) {
  // Two parts under ONE key. A shared nonce would make their keystreams
  // identical, and XORing the two ciphertexts would recover both plaintexts
  // with no key at all — a confidentiality assertion, not a cosmetic one.
  const std::string stem = "visio_schema_vrec_rot";
  RemoveParts(stem);
  {
    // Rotation is driven by PAYLOAD bytes, so the small fixture above would
    // never reach the cut; write past it deliberately.
    McapWriter w(TempPath(stem + ".mcap"), /*max_bytes=*/4096, 0.0, false, 0, 0,
                 TestKey(3));
    const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
    const std::string blob(2048, 'x');
    for (int i = 0; i < 8; ++i) w.Write(ch, Data(kFirstDynamic, blob));
    w.Close();
  }
  const std::string a = SlurpFile(PartPath(stem, 0));
  const std::string b = SlurpFile(PartPath(stem, 1));
  ASSERT_GE(a.size(), kVrecHeaderBytes);
  ASSERT_GE(b.size(), kVrecHeaderBytes);

  VrecHeader ha, hb;
  std::string err;
  ASSERT_TRUE(ParseVrecHeader(reinterpret_cast<const std::uint8_t*>(a.data()),
                              a.size(), &ha, &err));
  ASSERT_TRUE(ParseVrecHeader(reinterpret_cast<const std::uint8_t*>(b.data()),
                              b.size(), &hb, &err));
  EXPECT_NE(ha.nonce, hb.nonce);
  // ...but the same key, so one fingerprint opens the whole session.
  EXPECT_EQ(ha.key_fp, hb.key_fp);
  RemoveParts(stem);
}

TEST(McapWriterVrec, SyncSpansStillProduceIdenticalBytesWhenEncrypting) {
  // sync_span_bytes addresses the FILE while the cipher addresses the
  // PLAINTEXT stream, and the two differ by the 32-byte header. Get that
  // wrong and writeback spans skew; the bytes must not care either way.
  const std::string nosync = TempPath("visio_schema_vrec_nosync.mcap");
  const std::string synced = TempPath("visio_schema_vrec_synced.mcap");
  std::remove(nosync.c_str());
  std::remove(synced.c_str());
  const RecordingKey key = TestKey(23);
  {
    McapWriter w(nosync, 0, 0.0, false, 0, 0, key);
    WriteFixture(w);
  }
  {
    McapWriter w(synced, 0, 0.0, false, 0, /*sync_span_bytes=*/4096, key);
    WriteFixture(w);
  }
  // Nonces differ per part, so compare the decrypted plaintext, not the bytes.
  EXPECT_EQ(DecryptPart(SlurpFile(nosync), key),
            DecryptPart(SlurpFile(synced), key));
  std::remove(nosync.c_str());
  std::remove(synced.c_str());
}


// Replay the shape of the real 2704 -> 2734 omission: a remote/delayed
// non-IDR must survive a part cut, independently of producer IDR alignment.
TEST(McapWriterContinuation, ForcedCutPreservesEveryNonIdr) {
  RemoveParts("rotation_continuation");
  const Channel local = VideoChannel(kFirstDynamic, "/ego/camera/0");
  const Channel wrist = VideoChannel(kFirstDynamic + 1, "/wrist/camera/0");
  visio_schema::mcap::McapRotationPolicy policy;
  policy.local_video_ids = {local.id};
  McapWriter w(TempPath("rotation_continuation.mcap"), 40, 0, true, 500,
               0, std::nullopt, {}, {}, {}, policy);
  w.Write(local, VideoMsg(local.id, std::string(20, 'a'), true, 0));
  w.Write(wrist, VideoMsg(wrist.id, std::string(20, 'b'), false, 0));
  for (int i = 1; i <= 40; ++i)
    w.Write(local, VideoMsg(local.id, std::string(10, 'c'), false, i * 33333333LL, i));
  w.Close();
  EXPECT_EQ(w.bytes_written(), 440u);
  EXPECT_GT(w.rotation_stats().forced, 0u);
  const auto rows = ContinuationRows("rotation_continuation");
  ASSERT_EQ(rows.size(), 42u);
  EXPECT_EQ(rows[1], wrist.topic + ":0:0:" + std::string(20,'b'));
  for (std::size_t i = 2; i < rows.size(); ++i)
    EXPECT_EQ(rows[i], local.topic + ":" + std::to_string(i-1) + ":" + std::to_string((i-1)*33333333LL) + ":" + std::string(10,'c'));
  EXPECT_EQ(w.rotation_stats().startup_dropped, 0u);
  RemoveParts("rotation_continuation");
}

TEST(McapWriterContinuation, WaitsForLocalPairAndKeepsWristFrames) {
  RemoveParts("rotation_pair");
  const Channel a = VideoChannel(kFirstDynamic, "/ego/camera/0");
  const Channel b = VideoChannel(kFirstDynamic + 1, "/ego/camera/1");
  const Channel wrist = VideoChannel(kFirstDynamic + 2, "/wrist/camera/0");
  visio_schema::mcap::McapRotationPolicy policy;
  policy.local_video_ids = {a.id, b.id};
  McapWriter w(TempPath("rotation_pair.mcap"), 1000, 0, true, 500,
               0, std::nullopt, {}, {}, {}, policy);
  w.Write(a, VideoMsg(a.id, std::string(500, 'a'), true, 0));
  w.Write(b, VideoMsg(b.id, std::string(500, 'b'), true, 0));
  w.Write(a, VideoMsg(a.id, std::string(10, 'c'), true, 33333333));
  EXPECT_EQ(PartCount("rotation_pair"), 1);
  w.Write(wrist, VideoMsg(wrist.id, std::string(10, 'd'), false, 33333333));
  w.Write(b, VideoMsg(b.id, std::string(10, 'e'), false, 1000)); // late prior frame
  w.Write(b, VideoMsg(b.id, std::string(10, 'f'), true, 33341333));
  EXPECT_EQ(PartCount("rotation_pair"), 2);
  w.Close();
  EXPECT_EQ(w.bytes_written(), 1040u);
  EXPECT_EQ(w.rotation_stats().aligned, 1u);
  EXPECT_EQ(w.rotation_stats().forced, 0u);
  RemoveParts("rotation_pair");
}

TEST(McapWriterContinuation, StopCancelsIncompleteCutWithoutLosingStagedFrames) {
  RemoveParts("rotation_stop");
  const Channel a = VideoChannel(kFirstDynamic, "/a");
  const Channel b = VideoChannel(kFirstDynamic + 1, "/b");
  visio_schema::mcap::McapRotationPolicy policy;
  policy.local_video_ids = {a.id,b.id};
  McapWriter w(TempPath("rotation_stop.mcap"), 1000, 0, true, 500,
               0, std::nullopt, {}, {}, {}, policy);
  w.Write(a, VideoMsg(a.id, std::string(500,'a'), true, 0));
  w.Write(b, VideoMsg(b.id, std::string(500,'b'), true, 0));
  w.Write(a, VideoMsg(a.id, std::string(10,'c'), true, 33333333));
  w.Close();
  EXPECT_EQ(w.bytes_written(),1010u);
  EXPECT_EQ(PartCount("rotation_stop"),1);
  EXPECT_EQ(w.rotation_pending_bytes(),0u);
  RemoveParts("rotation_stop");
}

TEST(McapWriterContinuation, FullGroupSpreadMustFitTolerance) {
  RemoveParts("rotation_spread");
  const auto a = VideoChannel(16,"/a"), b = VideoChannel(17,"/b"), c = VideoChannel(18,"/c");
  visio_schema::mcap::McapRotationPolicy policy;
  policy.local_video_ids = {16,17,18}; policy.tolerance_ns = 10;
  McapWriter w(TempPath("rotation_spread.mcap"), 3000, 0, true, 0, 0, {}, {}, {}, {}, policy);
  for (auto ch : {a,b,c}) w.Write(ch,VideoMsg(ch.id,std::string(1000,'x'),true,0));
  w.Write(a,VideoMsg(16,"a",true,100));
  w.Write(b,VideoMsg(17,"b",true,91));
  w.Write(c,VideoMsg(18,"c",true,109));
  w.Close();
  EXPECT_EQ(w.rotation_stats().aligned,0u);
  EXPECT_EQ(ContinuationRows("rotation_spread").size(),6u);
  RemoveParts("rotation_spread");
}

TEST(McapWriterContinuation, TimeoutAndByteBoundPreserveEncryptedRecords) {
  for (const bool timeout : {false,true}) {
    RemoveParts("rotation_bounded");
    const auto a = VideoChannel(16,"/a"), b = VideoChannel(17,"/b");
    const auto imu = MakeChannel(18,"/imu");
    visio_schema::mcap::McapRotationPolicy policy;
    policy.local_video_ids = {16,17}; policy.max_staged_bytes = 1024;
    policy.max_wait = std::chrono::milliseconds(2);
    McapWriter w(TempPath("rotation_bounded.mcap"), 10000, 0, true, 0, 0, TestKey(3), {}, {}, {}, policy);
    w.Write(a,VideoMsg(16,std::string(5000,'a'),true,0));
    w.Write(b,VideoMsg(17,std::string(5000,'b'),true,0));
    w.Write(a,VideoMsg(16,"idr",true,33333333));
    ASSERT_GT(w.rotation_pending_bytes(),0u);
    if (timeout) { std::this_thread::sleep_for(std::chrono::milliseconds(4)); w.FlushDue(); }
    w.Write(imu,Data(18,std::string(1500,'m')));
    w.Write(b,VideoMsg(17,"delta",false,33341333));
    w.Close();
    const auto rows = ContinuationRows("rotation_bounded",TestKey(3));
    ASSERT_EQ(rows.size(),5u);
    EXPECT_EQ(rows[2],"/a:0:33333333:idr");
    EXPECT_EQ(rows[4],"/b:0:33341333:delta");
    EXPECT_EQ(w.rotation_pending_bytes(),0u);
    EXPECT_LE(w.rotation_stats().max_staged_bytes,1024u);
    EXPECT_GT(w.rotation_stats().cancelled,0u);
    RemoveParts("rotation_bounded");
  }
}

TEST(McapWriterContinuation, ThousandPartsRetainExactMixedCameraRecords) {
  const std::string stem="rotation_many_parts";
  RemoveParts(stem);
  const auto a=VideoChannel(16,"/a"), b=VideoChannel(17,"/b"), wrist=VideoChannel(18,"/wrist");
  visio_schema::mcap::McapRotationPolicy policy; policy.local_video_ids={16,17};
  McapWriter writer(TempPath(stem+".mcap"),2048,0,true,0,0,{}, {}, {}, {},policy);
  std::vector<std::string> expected;
  for (unsigned i=0;i<10000;++i) {
    const auto& channel=i%3==0 ? a : i%3==1 ? b : wrist;
    const auto seq=i/3;
    const auto timestamp=seq*33333333LL+(i%3)*8000;
    auto message=VideoMsg(channel.id,std::string(256,static_cast<char>(32+seq%80)),
                          i%3!=2 && seq%30==0,timestamp,seq);
    expected.push_back(channel.topic+":"+std::to_string(seq)+":"+std::to_string(timestamp)+":"+message.payload.str());
    writer.Write(channel,message);
  }
  writer.Close();
  EXPECT_GT(PartCount(stem),1000);
  auto actual=ContinuationRows(stem);
  std::sort(expected.begin(),expected.end());
  std::sort(actual.begin(),actual.end());
  EXPECT_EQ(actual,expected);
  EXPECT_EQ(writer.bytes_written(),10000u*256);
  EXPECT_LE(writer.rotation_stats().max_staged_bytes,policy.max_staged_bytes);
  EXPECT_EQ(writer.rotation_stats().startup_dropped,0u);
  RemoveParts(stem);
}

TEST(McapWriterContinuation, LocalStartupGateSurvivesAnEarlyForcedCut) {
  const std::string stem="rotation_initial_gate"; RemoveParts(stem);
  const auto a=VideoChannel(16,"/a"), b=VideoChannel(17,"/b"), wrist=VideoChannel(18,"/wrist");
  visio_schema::mcap::McapRotationPolicy policy;policy.local_video_ids={16,17};
  McapWriter writer(TempPath(stem+".mcap"),128,0,true,0,0,{}, {}, {}, {},policy);
  writer.Write(a,VideoMsg(16,std::string(128,'a'),true,0));
  writer.Write(wrist,VideoMsg(18,std::string(128,'w'),false,0));
  writer.Write(b,VideoMsg(17,std::string(128,'p'),false,33333333,0));
  writer.Write(b,VideoMsg(17,std::string(128,'b'),true,66666666,1));
  writer.Close();
  EXPECT_EQ(writer.rotation_stats().startup_dropped,1u);
  EXPECT_EQ(writer.bytes_written(),384u);
  const auto rows=ContinuationRows(stem);
  ASSERT_EQ(rows.size(),3u);
  EXPECT_EQ(rows[2],"/b:1:66666666:"+std::string(128,'b'));
  RemoveParts(stem);
}

TEST(McapWriterContinuation, DurationGraceCannotBeRenewedByUnpairedIdrs) {
  const std::string stem="rotation_duration_grace";RemoveParts(stem);
  const auto a=VideoChannel(16,"/a"), b=VideoChannel(17,"/b");
  visio_schema::mcap::McapRotationPolicy policy;policy.local_video_ids={16,17};
  policy.max_wait=std::chrono::milliseconds(2);
  McapWriter writer(TempPath(stem+".mcap"),0,0.002,true,0,0,{}, {}, {}, {},policy);
  writer.Write(a,VideoMsg(16,std::string(500,'a'),true,0));
  writer.Write(b,VideoMsg(17,std::string(500,'b'),true,0));
  std::this_thread::sleep_for(std::chrono::milliseconds(20));
  writer.Write(a,VideoMsg(16,std::string(500,'c'),true,33333333,1));
  writer.Close();
  EXPECT_GT(writer.rotation_stats().forced,0u);
  EXPECT_GT(PartCount(stem),1);
  EXPECT_EQ(writer.bytes_written(),1500u);
  EXPECT_EQ(ContinuationRows(stem).size(),3u);
  RemoveParts(stem);
}
