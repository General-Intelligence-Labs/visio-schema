// Fixture for the write-time read-back suites (test_mcap_readback.cc,
// test_mcap_readback_faults.cc): McapWriter with McapReadbackOptions, the
// stepping driven from the test thread (the library creates none), and
// the field failure's exact block for injection.
//
// /tmp may be tmpfs, where O_DIRECT is refused and the read-back falls
// back to buffered reads with page-cache eviction. Every assertion in the
// suites holds on both paths — the injected corruption is in the file
// either way — and the fallback notice is the one log line tolerated. Not
// testable here: a failing sync_file_range (OnSyncDisabled) and a genuine
// medium-side fault, which the board test owns.
#pragma once

#include "visio_schema/mcap/writer.hpp"

#include <gtest/gtest.h>

#include <fcntl.h>
#include <unistd.h>

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include "mcap_writer_test_util.hpp"
#include "visio_schema/wire/control.hpp"

using namespace mcap_test;
using visio_schema::Channel;
using visio_schema::kFirstDynamic;
using visio_schema::mcap::McapReadbackOptions;
using visio_schema::mcap::McapReadbackPass;
using visio_schema::mcap::McapReadbackStats;
using visio_schema::mcap::McapWriter;
using visio_schema::mcap::RecordingKey;
namespace fs = std::filesystem;

namespace mcap_readback_test {

using std::chrono::milliseconds;

constexpr std::uint64_t kSyncSpan = 64 * 1024;
constexpr std::uint64_t kRotateBytes = 1024 * 1024;  // payload per part
constexpr int kMessages = 1024;                     // x 3000 B, ~3 MB
constexpr std::size_t kPayloadBytes = 3000;
// The field failure: a 31-byte high-entropy value + 158 zeros, sector
// terminated, at byte 7491 of the cluster.
constexpr std::uint64_t kStaleBlockOffset = 7491;
constexpr std::size_t kStaleBlockBytes = 189;
constexpr std::size_t kStaleEntropyBytes = 31;

inline McapReadbackOptions TestOptions() {
  McapReadbackOptions o;
  o.ring_bytes = 4 << 20;    // everything stays resident
  o.settle_bytes = 0;        // a span is ready as soon as it is evicted
  o.piece_bytes = 64 << 10;  // several pieces per span, exercising the loop
  o.close_flush_ms = 5000;   // Close verifies the last tail
  return o;
}

// Varying bytes, so a misplaced ring window cannot pass as a match the way
// a constant fill would.
inline std::string Payload(int m) {
  std::string p(kPayloadBytes, '\0');
  for (std::size_t i = 0; i < p.size(); ++i)
    p[i] = static_cast<char>((m * 31 + i) & 0xff);
  return p;
}

struct Outcome {
  McapReadbackStats stats;
  bool storage_fault;
};

// What the card did: the block appears in the file behind the writer's
// back, through a fd of its own.
inline void InjectBytes(const std::string& path, std::uint64_t at,
                 const std::uint8_t* bytes, std::size_t n) {
  const int fd = ::open(path.c_str(), O_WRONLY | O_CLOEXEC);
  ASSERT_GE(fd, 0) << path;
  EXPECT_EQ(::pwrite(fd, bytes, n, static_cast<off_t>(at)),
            static_cast<ssize_t>(n));
  EXPECT_EQ(::fsync(fd), 0);
  ::close(fd);
}

inline void InjectStaleBlock(const std::string& path, std::uint64_t at) {
  std::uint8_t block[kStaleBlockBytes] = {};
  for (std::size_t i = 0; i < kStaleEntropyBytes; ++i)
    block[i] = static_cast<std::uint8_t>(0xA5 ^ (i * 37));
  InjectBytes(path, at, block, sizeof block);
}

inline bool SpanCanHoldTheBlock(std::uint64_t len) {
  return len >= kStaleBlockOffset + kStaleBlockBytes;
}

inline std::uint64_t ReportedMismatchOffset(const std::string& log) {
  static const char kKey[] = "mismatch at ";
  const std::size_t p = log.find(kKey);
  if (p == std::string::npos) return UINT64_MAX;
  return std::strtoull(log.c_str() + p + sizeof(kKey) - 1, nullptr, 10);
}

inline std::size_t Count(const std::string& hay, const std::string& needle) {
  std::size_t n = 0;
  for (std::size_t p = hay.find(needle); p != std::string::npos;
       p = hay.find(needle, p + needle.size()))
    ++n;
  return n;
}

inline int PartIndexOf(const std::string& stem, const std::string& path) {
  for (int i = 0, n = PartCount(stem); i < n; ++i)
    if (PartPath(stem, i) == path) return i;
  return -1;
}

}  // namespace mcap_readback_test

using namespace mcap_readback_test;

class McapReadback : public ::testing::Test {
 protected:
  void TearDown() override {
    for (const std::string& s : stems_) RemoveParts(s);
  }
  std::string Stem(const std::string& tag) {
    stems_.push_back("visio_rb_" + tag + "_" + std::to_string(::getpid()));
    RemoveParts(stems_.back());
    return stems_.back();
  }
  // kMessages of Payload() rotated every max_bytes, stepping the read-back
  // from this thread every `step_every` writes (0 = never: Close's bounded
  // flush is then the only stepping). Returns the stats after Close.
  Outcome Record(const std::string& stem,
                 const std::optional<RecordingKey>& key,
                 McapReadbackOptions opts, int step_every,
                 int messages = kMessages,
                 std::uint64_t max_bytes = kRotateBytes) {
    const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
    McapWriter w(TempPath(stem + ".mcap"), max_bytes, 0.0, false, 0,
                 kSyncSpan, key, std::move(opts));
    for (int i = 0; i < messages; ++i) {
      w.Write(ch, Data(kFirstDynamic, Payload(i)));
      if (step_every > 0 && i % step_every == 0) {
        while (w.ReadbackStep(milliseconds(50))) {
        }
      }
    }
    w.Close();
    EXPECT_EQ(w.readback_pending(), 0u);
    return Outcome{w.readback_stats(), w.storage_fault()};
  }
  // A reference recording with read-back off, for byte comparisons.
  std::string Reference(const std::string& tag,
                        const std::optional<RecordingKey>& key,
                        int messages = kMessages) {
    const std::string stem = Stem(tag + "_ref");
    Record(stem, key, McapReadbackOptions{}, 0, messages);
    return stem;
  }
  // Every part byte-identical (plaintext) or plaintext-identical (VREC:
  // nonces differ per part, so the ciphertext legitimately does). A
  // non-negative `differing_part` says one part is EXPECTED to differ.
  void ExpectSameRecording(const std::string& a, const std::string& b,
                           const std::optional<RecordingKey>& key,
                           int differing_part = -1) {
    const int n = PartCount(a);
    ASSERT_GE(n, 2) << "expected rotation";
    ASSERT_EQ(PartCount(b), n);
    for (int i = 0; i < n; ++i) {
      std::string pa = SlurpFile(PartPath(a, i));
      std::string pb = SlurpFile(PartPath(b, i));
      if (key) {
        pa = DecryptPart(pa, *key);
        pb = DecryptPart(pb, *key);
      }
      EXPECT_TRUE(EndsWithMcapMagic(pb)) << "part " << i << " not finalized";
      EXPECT_EQ(pa == pb, i != differing_part) << "part " << i;
    }
  }

 private:
  std::vector<std::string> stems_;
};

