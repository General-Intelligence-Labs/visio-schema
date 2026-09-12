// Write-time read-back: what becomes of a span that is wrong on the
// medium, and of one whose verdict is interrupted. Every path that leaves
// the medium known-wrong must end in storage_fault(); only a span never
// seen wrong may be skipped or counted as a failed read.
#include "mcap_readback_test_util.hpp"

#include <algorithm>

TEST_F(McapReadback,
       PersistentlyBadSectorLatchesStorageFaultAndRecordingContinues) {
  const std::string ref = Reference("bad", std::nullopt);
  const std::string on = Stem("bad_on");
  McapReadbackOptions o = TestOptions();
  std::optional<std::pair<std::string, std::uint64_t>> target;
  std::uint64_t bad_len = 0;
  int injections = 0;
  o.before_span_read_for_test = [&](const std::string& path,
                                    std::uint64_t off, std::uint64_t len,
                                    McapReadbackPass) {
    if (!target) {
      if (!SpanCanHoldTheBlock(len)) return;
      target = std::make_pair(path, off);
      bad_len = len;
    }
    if (path != target->first || off != target->second) return;
    ++injections;  // the first read, then again after the rewrite
    InjectStaleBlock(path, off + kStaleBlockOffset);
  };
  testing::internal::CaptureStderr();
  const Outcome out = Record(on, std::nullopt, o, 16);
  const std::string log = testing::internal::GetCapturedStderr();

  ASSERT_TRUE(target.has_value());
  EXPECT_EQ(injections, 2);
  EXPECT_EQ(out.stats.spans_mismatched, 1u);
  EXPECT_EQ(out.stats.spans_rewritten_ok, 0u);
  EXPECT_EQ(out.stats.spans_unrepaired, 1u);
  EXPECT_EQ(out.stats.spans_skipped, 0u);
  EXPECT_TRUE(out.storage_fault);
  EXPECT_EQ(Count(log, "storage fault"), 1u) << log;
  // The recording went on: later spans were verified and every part is
  // complete; only the part with the bad block differs from the reference.
  EXPECT_GT(out.stats.spans_verified, 3u);
  EXPECT_EQ(out.stats.bytes_verified, PartsBytes(on) - bad_len);
  EXPECT_EQ(PartsBytes(on), PartsBytes(ref));
  const int bad_part = PartIndexOf(on, target->first);
  ASSERT_GE(bad_part, 0);
  ExpectSameRecording(ref, on, std::nullopt, bad_part);
}

TEST_F(McapReadback, TwoBadSpansAreTwoUnrepairedButOneStorageFaultLine) {
  const std::string on = Stem("twobad_on");
  McapReadbackOptions o = TestOptions();
  std::vector<std::pair<std::string, std::uint64_t>> targets;
  o.before_span_read_for_test = [&](const std::string& path,
                                    std::uint64_t off, std::uint64_t len,
                                    McapReadbackPass pass) {
    const auto hit = std::make_pair(path, off);
    const bool known =
        std::find(targets.begin(), targets.end(), hit) != targets.end();
    if (!known) {
      if (targets.size() == 2 || pass != McapReadbackPass::kFirstRead ||
          !SpanCanHoldTheBlock(len))
        return;
      targets.push_back(hit);
    }
    InjectStaleBlock(path, off + kStaleBlockOffset);
  };
  testing::internal::CaptureStderr();
  const Outcome out = Record(on, std::nullopt, o, 16);
  const std::string log = testing::internal::GetCapturedStderr();
  ASSERT_EQ(targets.size(), 2u);
  EXPECT_EQ(out.stats.spans_unrepaired, 2u);
  EXPECT_EQ(Count(log, "leaving it"), 2u) << log;
  EXPECT_EQ(Count(log, "storage fault"), 1u) << log;
  EXPECT_TRUE(out.storage_fault);
}

// A read the medium cannot serve AFTER a rewrite: the medium is known
// wrong, so the verdict is a storage fault, never a quiet skip.
TEST_F(McapReadback, AReadFailureAfterTheRewriteIsAStorageFault) {
  const std::string on = Stem("readfail_rw");
  McapReadbackOptions o = TestOptions();
  bool armed = false;
  o.before_span_read_for_test = [&](const std::string& path,
                                    std::uint64_t off, std::uint64_t len,
                                    McapReadbackPass pass) {
    // The tail of a closed part: the writer is done with that file, so
    // cutting it short is safe. First read: plant a block; the re-read:
    // cut the file below the span.
    if (!armed && pass == McapReadbackPass::kFirstRead && len % 4096 != 0 &&
        SpanCanHoldTheBlock(len)) {
      armed = true;
      InjectStaleBlock(path, off + kStaleBlockOffset);
      return;
    }
    if (armed && pass == McapReadbackPass::kReadAfterRewrite)
      EXPECT_EQ(::truncate(path.c_str(), static_cast<off_t>(off)), 0);
  };
  testing::internal::CaptureStderr();
  const Outcome out = Record(on, std::nullopt, o, 16);
  const std::string log = testing::internal::GetCapturedStderr();
  ASSERT_TRUE(armed);
  EXPECT_EQ(out.stats.spans_mismatched, 1u);
  EXPECT_EQ(out.stats.spans_rewritten_ok, 0u);
  EXPECT_EQ(out.stats.spans_unrepaired, 1u);
  EXPECT_TRUE(out.storage_fault);
  EXPECT_NE(log.find("short file"), std::string::npos) << log;
}

// A read that comes up short on a span never seen wrong is counted, logged
// and not a verdict.
TEST_F(McapReadback, AShortReadOnAFreshSpanIsCountedAndLogged) {
  const std::string on = Stem("readfail_fresh");
  McapReadbackOptions o = TestOptions();
  bool cut = false;
  o.before_span_read_for_test = [&](const std::string& path,
                                    std::uint64_t off, std::uint64_t len,
                                    McapReadbackPass) {
    if (cut || len % 4096 == 0) return;  // a closed part's tail
    cut = true;
    EXPECT_EQ(::truncate(path.c_str(), static_cast<off_t>(off)), 0);
  };
  testing::internal::CaptureStderr();
  const Outcome out = Record(on, std::nullopt, o, 16);
  const std::string log = testing::internal::GetCapturedStderr();
  ASSERT_TRUE(cut);
  EXPECT_EQ(out.stats.read_failed, 1u);
  EXPECT_EQ(out.stats.spans_mismatched, 0u);
  EXPECT_EQ(out.stats.spans_unrepaired, 0u);
  EXPECT_FALSE(out.storage_fault);
  EXPECT_NE(log.find("read failed: short file"), std::string::npos) << log;
}

