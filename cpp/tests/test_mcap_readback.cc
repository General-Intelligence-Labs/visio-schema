// Write-time read-back: the happy paths, the ring and the stepping
// contract. The verdict paths are test_mcap_readback_faults.cc.
#include "mcap_readback_test_util.hpp"

#include <algorithm>

TEST_F(McapReadback, NeverAltersTheBytes) {
  for (const bool encrypted : {false, true}) {
    SCOPED_TRACE(encrypted ? "VREC" : "plaintext");
    const std::optional<RecordingKey> key =
        encrypted ? std::optional<RecordingKey>(TestKey(5)) : std::nullopt;
    const std::string ref = Reference("same", key);
    const std::string on = Stem("same_on");
    testing::internal::CaptureStderr();
    const Outcome out = Record(on, key, TestOptions(), 16);
    const std::string log = testing::internal::GetCapturedStderr();
    EXPECT_EQ(log.find("mismatch at"), std::string::npos) << log;
    EXPECT_EQ(log.find("storage fault"), std::string::npos) << log;
    EXPECT_EQ(Count(log, "mcap readback: "), 1u) << "the totals line: " << log;
    ExpectSameRecording(ref, on, key);
    // Spans and tails tile every part exactly once, and nothing was skipped.
    EXPECT_GT(out.stats.spans_verified, 3u);
    EXPECT_EQ(out.stats.bytes_verified, PartsBytes(on));
    EXPECT_EQ(out.stats.spans_mismatched, 0u);
    EXPECT_EQ(out.stats.spans_skipped, 0u);
    EXPECT_EQ(out.stats.read_failed, 0u);
    EXPECT_FALSE(out.storage_fault);
  }
}

// The default, which is what the firmware passes today: nothing runs, the
// seam never fires, a step is a no-op.
TEST_F(McapReadback, DefaultOffIsInert) {
  McapReadbackOptions off;
  int seam_calls = 0;
  off.before_span_read_for_test = [&](const std::string&, std::uint64_t,
                                      std::uint64_t, McapReadbackPass) {
    ++seam_calls;
  };
  const std::string stem = Stem("off");
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  McapWriter w(TempPath(stem + ".mcap"), kRotateBytes, 0.0, false, 0,
               kSyncSpan, std::nullopt, off);
  for (int i = 0; i < 256; ++i) w.Write(ch, Data(kFirstDynamic, Payload(i)));
  EXPECT_FALSE(w.ReadbackStep(milliseconds(1000)));
  EXPECT_EQ(w.readback_pending(), 0u);
  w.Close();
  EXPECT_EQ(seam_calls, 0);
  const McapReadbackStats st = w.readback_stats();
  EXPECT_EQ(st.spans_verified + st.spans_skipped + st.spans_mismatched +
                st.read_failed,
            0u);
  EXPECT_FALSE(w.storage_fault());
}

TEST_F(McapReadback, DetectsAndRewritesAnInjectedStaleSector) {
  for (const bool encrypted : {false, true}) {
    SCOPED_TRACE(encrypted ? "VREC" : "plaintext");
    const std::optional<RecordingKey> key =
        encrypted ? std::optional<RecordingKey>(TestKey(7)) : std::nullopt;
    const std::string ref = Reference("stale", key);
    const std::string on = Stem("stale_on");
    McapReadbackOptions o = TestOptions();
    std::optional<std::uint64_t> injected_at;
    o.before_span_read_for_test = [&](const std::string& path,
                                      std::uint64_t off, std::uint64_t len,
                                      McapReadbackPass pass) {
      if (injected_at || pass != McapReadbackPass::kFirstRead ||
          !SpanCanHoldTheBlock(len))
        return;
      injected_at = off + kStaleBlockOffset;
      InjectStaleBlock(path, off + kStaleBlockOffset);
    };
    testing::internal::CaptureStderr();
    const Outcome out = Record(on, key, o, 16);
    const std::string log = testing::internal::GetCapturedStderr();

    ASSERT_TRUE(injected_at.has_value());
    EXPECT_EQ(out.stats.spans_mismatched, 1u);
    EXPECT_EQ(out.stats.spans_rewritten_ok, 1u);
    EXPECT_EQ(out.stats.spans_unrepaired, 0u);
    EXPECT_EQ(out.stats.spans_skipped, 0u);
    EXPECT_EQ(out.stats.bytes_verified, PartsBytes(on));  // repaired: once
    EXPECT_FALSE(out.storage_fault);
    // One line names the first differing byte (inside the high-entropy
    // head of the block; a leading byte may coincide with the original)...
    const std::uint64_t reported = ReportedMismatchOffset(log);
    EXPECT_GE(reported, *injected_at) << log;
    EXPECT_LT(reported, *injected_at + kStaleEntropyBytes) << log;
    EXPECT_EQ(Count(log, "rewritten and verified"), 1u) << log;
    EXPECT_EQ(log.find("storage fault"), std::string::npos) << log;
    // ...and the rewrite restored the file to what the writer produces
    // with read-back off.
    ExpectSameRecording(ref, on, key);
  }
}

// The VREC header is on-disk bytes like any other: a hit there is caught
// and repaired from the ring too.
TEST_F(McapReadback, TheVrecHeaderIsCovered) {
  const std::optional<RecordingKey> key(TestKey(11));
  const std::string ref = Reference("hdr", key);
  const std::string on = Stem("hdr_on");
  McapReadbackOptions o = TestOptions();
  bool injected = false;
  o.before_span_read_for_test = [&](const std::string& path,
                                    std::uint64_t off, std::uint64_t,
                                    McapReadbackPass pass) {
    if (injected || off != 0 || pass != McapReadbackPass::kFirstRead) return;
    injected = true;
    const std::uint8_t junk[8] = {1, 2, 3, 4, 5, 6, 7, 8};
    InjectBytes(path, 4, junk, sizeof junk);
  };
  testing::internal::CaptureStderr();
  const Outcome out = Record(on, key, o, 16);
  const std::string log = testing::internal::GetCapturedStderr();
  ASSERT_TRUE(injected);
  EXPECT_EQ(out.stats.spans_rewritten_ok, 1u);
  EXPECT_LT(ReportedMismatchOffset(log), 32u) << log;
  ExpectSameRecording(ref, on, key);
}

// Detection fires on the first piece; only a WHOLE-span rewrite makes the
// re-read pass when the last piece is bad too.
TEST_F(McapReadback, TheWholeSpanIsRewrittenNotJustTheFirstPiece) {
  const std::string ref = Reference("whole", std::nullopt);
  const std::string on = Stem("whole_on");
  McapReadbackOptions o = TestOptions();
  bool injected = false;
  o.before_span_read_for_test = [&](const std::string& path,
                                    std::uint64_t off, std::uint64_t len,
                                    McapReadbackPass pass) {
    if (injected || pass != McapReadbackPass::kFirstRead ||
        len < 2 * o.piece_bytes)
      return;
    injected = true;
    InjectStaleBlock(path, off + kStaleBlockOffset);
    InjectStaleBlock(path, off + len - 8192 + 100);
  };
  const Outcome out = Record(on, std::nullopt, o, 16);
  ASSERT_TRUE(injected);
  EXPECT_EQ(out.stats.spans_mismatched, 1u);
  EXPECT_EQ(out.stats.spans_rewritten_ok, 1u);
  EXPECT_EQ(out.stats.spans_unrepaired, 0u);
  ExpectSameRecording(ref, on, std::nullopt);
}

// One step: one piece at least, one span at most, resuming across calls.
TEST_F(McapReadback, AStepDoesOnePieceAtLeastAndOneSpanAtMost) {
  const std::optional<RecordingKey> key(TestKey(3));  // 64 KiB spans
  const std::string stem = Stem("budget");
  McapReadbackOptions o = TestOptions();
  o.piece_bytes = 4096;  // 16 pieces per span
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  McapWriter w(TempPath(stem + ".mcap"), kRotateBytes, 0.0, false, 0,
               kSyncSpan, key, o);
  for (int i = 0; i < 300; ++i) w.Write(ch, Data(kFirstDynamic, Payload(i)));
  const std::size_t queued = w.readback_pending();
  ASSERT_GE(queued, 4u);
  int steps = 0;
  while (w.readback_pending() == queued) {
    EXPECT_TRUE(w.ReadbackStep(milliseconds(0)));
    ++steps;
  }
  EXPECT_EQ(steps, 16) << "a zero budget is one piece per call";
  EXPECT_TRUE(w.ReadbackStep(milliseconds(10000)));
  EXPECT_EQ(w.readback_pending(), queued - 2) << "one span per call";
  w.Close();
  EXPECT_FALSE(w.ReadbackStep(milliseconds(10)));
}

// settle_bytes holds a span until the writer is that far past it; Close
// waives the wait and verifies everything.
TEST_F(McapReadback, SettleBytesHoldSpansUntilTheWriterIsPastThem) {
  const std::string on = Stem("settle_on");
  McapReadbackOptions o = TestOptions();
  o.settle_bytes = 64 << 20;
  const Channel ch = MakeChannel(kFirstDynamic, "/dev/imu/0/raw");
  McapWriter w(TempPath(on + ".mcap"), kRotateBytes, 0.0, false, 0,
               kSyncSpan, std::nullopt, o);
  bool ever_pending = false;
  for (int i = 0; i < kMessages; ++i) {
    w.Write(ch, Data(kFirstDynamic, Payload(i)));
    ever_pending = ever_pending || w.readback_pending() > 0;
    EXPECT_FALSE(w.ReadbackStep(milliseconds(10)));
  }
  EXPECT_TRUE(ever_pending);
  w.Close();
  const McapReadbackStats st = w.readback_stats();
  EXPECT_EQ(st.spans_skipped, 0u);
  EXPECT_EQ(st.bytes_verified, PartsBytes(on));
  EXPECT_GT(st.max_lag_bytes, 0u);
}

TEST_F(McapReadback, RingOverrunSkipsNeverBlocks) {
  // A ring smaller than what is in flight: plaintext spans (~768 KiB mcap
  // chunks) never fit and are skipped on post; VREC spans (64 KiB) fit but
  // are overrun by the next span before they are stepped. Either way the
  // writer finishes untouched and the bytes are what they would be with
  // read-back off.
  for (const bool encrypted : {false, true}) {
    SCOPED_TRACE(encrypted ? "VREC" : "plaintext");
    const std::optional<RecordingKey> key =
        encrypted ? std::optional<RecordingKey>(TestKey(9)) : std::nullopt;
    const std::string ref = Reference("over", key);
    const std::string on = Stem("over_on");
    McapReadbackOptions o = TestOptions();
    o.ring_bytes = 64 << 10;
    o.close_flush_ms = 0;
    const Outcome out = Record(on, key, o, 16);
    EXPECT_GT(out.stats.spans_skipped, 0u);
    EXPECT_EQ(out.stats.spans_verified, 0u);
    EXPECT_EQ(out.stats.spans_mismatched, 0u);
    EXPECT_EQ(out.stats.spans_unrepaired, 0u);
    EXPECT_FALSE(out.storage_fault);
    ExpectSameRecording(ref, on, key);
  }
  // A stepper that never runs: the queue fills, the writer drops the rest
  // on the floor (counted) and never waits for anyone.
  const std::optional<RecordingKey> key(TestKey(13));
  const std::string ref = Reference("nostep", key, 2 * kMessages);
  const std::string on = Stem("nostep_on");
  McapReadbackOptions o = TestOptions();
  o.close_flush_ms = 0;
  const Outcome out = Record(on, key, o, 0, 2 * kMessages);
  EXPECT_EQ(out.stats.spans_verified, 0u);
  EXPECT_GT(out.stats.spans_skipped, 64u);
  EXPECT_FALSE(out.storage_fault);
  ExpectSameRecording(ref, on, key);
}

TEST_F(McapReadback, TailIsVerifiedAfterClose) {
  // Parts far smaller than a writeback span: nothing is evicted mid-part,
  // so each part's whole content is its tail, queued only after the part's
  // fsync — at the Roll for rotated parts, at Close for the last.
  auto record = [&](const std::string& tag, int close_flush_ms) {
    const std::string stem = Stem(tag);
    McapReadbackOptions o = TestOptions();
    o.close_flush_ms = close_flush_ms;
    const Outcome out =
        Record(stem, std::nullopt, o, 0, /*messages=*/24, 32 * 1024);
    return std::make_pair(stem, out);
  };
  const auto [flushed_stem, flushed] = record("tail_flushed", 5000);
  const int parts = PartCount(flushed_stem);
  ASSERT_GE(parts, 2);
  EXPECT_EQ(flushed.stats.spans_verified, static_cast<std::uint64_t>(parts));
  EXPECT_EQ(flushed.stats.bytes_verified, PartsBytes(flushed_stem));
  EXPECT_EQ(flushed.stats.spans_skipped, 0u);
  EXPECT_FALSE(flushed.storage_fault);
  // With no close budget the tails are counted, never waited for.
  const auto [dropped_stem, dropped] = record("tail_dropped", 0);
  EXPECT_EQ(dropped.stats.spans_verified, 0u);
  EXPECT_EQ(dropped.stats.spans_skipped, static_cast<std::uint64_t>(parts));
  // A budget too small for all of them: every tail is either verified or
  // counted skipped, nothing is lost in between.
  const auto [bounded_stem, bounded] = record("tail_bounded", 1);
  EXPECT_EQ(bounded.stats.spans_verified + bounded.stats.spans_skipped,
            static_cast<std::uint64_t>(parts));
}
