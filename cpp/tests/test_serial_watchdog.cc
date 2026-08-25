// SerialWatchdog — the I/O-free state machine that decides when /dev/ttyGS0 must
// be reopened (CONFIGURED edge / drain stall / retry-while-down), driven here
// deterministically with injected inputs.
#include "visio_schema/transport/serial_watchdog.hpp"

#include <gtest/gtest.h>

using visio_schema::transport::SerialWatchdog;
using Action = visio_schema::transport::SerialWatchdog::Action;

TEST(SerialWatchdog, RetriesWhileLinkDown) {
  SerialWatchdog wd;
  // No client open → periodic retry; the first tick fires immediately.
  EXPECT_EQ(wd.tick("", 0, /*accepted=*/0, /*client_open=*/false, 0), Action::ReopenRetry);
}

TEST(SerialWatchdog, RetryIsRateLimited) {
  SerialWatchdog wd;
  EXPECT_EQ(wd.tick("", 0, 0, false, 0), Action::ReopenRetry);
  // A second retry within kBaseRetryMs (10 s) is suppressed.
  EXPECT_EQ(wd.tick("", 0, 0, false, 5000), Action::None);
  // Past the interval it fires again.
  EXPECT_EQ(wd.tick("", 0, 0, false, 11000), Action::ReopenRetry);
}

TEST(SerialWatchdog, HealthyDrainingLinkIsQuiet) {
  SerialWatchdog wd;
  for (int i = 0; i < 5; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 0, /*accepted=*/0, /*client_open=*/true, i * 1000),
              Action::None);
  }
}

TEST(SerialWatchdog, ReopensOnConfiguredEdge) {
  SerialWatchdog wd;
  EXPECT_EQ(wd.tick("CONNECTED", 0, 0, true, 0), Action::None);  // seeds prev state
  EXPECT_EQ(wd.tick("CONFIGURED", 0, 0, true, 1000), Action::ReopenEdge);
}

TEST(SerialWatchdog, ReopensOnDrainStall) {
  SerialWatchdog wd;
  // A reader must first be observed ACCEPTING bytes so the later stall is a
  // stale-fd signal, not just "nobody ever read".
  EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 20, SerialWatchdog::kReaderProofBytes, true, 1000), Action::None);  // read → had_reader
  // Now pending sticks (non-decreasing) for kStallTicks while CONFIGURED and
  // the fd takes nothing more.
  EXPECT_EQ(wd.tick("CONFIGURED", 100, SerialWatchdog::kReaderProofBytes, true, 2000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, SerialWatchdog::kReaderProofBytes, true, 3000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, SerialWatchdog::kReaderProofBytes, true, 4000), Action::ReopenStalled);
}

TEST(SerialWatchdog, NoReaderDoesNotReopenOnStall) {
  SerialWatchdog wd;
  // Pending stuck high but NEVER drained (no host reading /dev/ttyGS0) — must
  // NOT reopen (that churn + blocking gs_close was the original bug).
  for (int i = 0; i < 10; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, i * 1000), Action::None);
  }
}

TEST(SerialWatchdog, OutboxEvictingItsOwnBacklogIsNotAReader) {
  // The production bug, in the shape the board actually produced it. A bounded
  // outbox evicts its own oldest frames to stay bounded, so `pending` FALLS with
  // nothing attached to /dev/ttyGS0. The old gate read that fall as "a reader
  // drained us", armed the stall path, and reopened every ~9 s forever — and
  // since each reopen clears the endpoint's stall latch, full-rate dual-eye
  // video refilled the outbox each cycle (660,831 frames door-dropped in one
  // 40-minute boot). Bytes accepted stay at zero throughout: nobody is reading.
  SerialWatchdog wd;
  std::size_t pending = 0;
  for (int i = 0; i < 40; ++i) {
    // Sawtooth: fills, evicts, fills again — never a byte accepted.
    pending = (i % 4 == 3) ? 20 : 100 + i;
    EXPECT_EQ(wd.tick("CONFIGURED", pending, /*accepted=*/0, true, i * 1000),
              Action::None)
        << "reopened at tick " << i << " for an outbox that evicted itself";
  }
}

TEST(SerialWatchdog, AReaderThatStopsStillStallsWhileTheOutboxEvicts) {
  // The mirror of the gate bug, and the half that was nearly shipped. Once a
  // real reader has been seen, a link that takes nothing more must still be
  // reopened EVEN THOUGH its outbox is evicting underneath it -- pending
  // sawtooths down on every eviction, and the old detector read each of those
  // falls as a drain and reset the stall counter. A stale fd whose backlog
  // self-evicted more often than kStallTicks would then never be reopened at
  // all. `accepted` is frozen throughout: nothing is reaching the wire.
  SerialWatchdog wd;
  const std::uint64_t seen = SerialWatchdog::kReaderProofBytes;
  EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, seen, true, 1000), Action::None);
  // Reader gone. Pending falls on eviction, rises as video refills.
  EXPECT_EQ(wd.tick("CONFIGURED", 20, seen, true, 2000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, seen, true, 3000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 30, seen, true, 4000), Action::ReopenStalled)
      << "an evicting outbox masked a stale fd";
}

TEST(SerialWatchdog, DrainingResetsStallCounter) {
  SerialWatchdog wd;
  EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 50, SerialWatchdog::kReaderProofBytes, true, 1000), Action::None);
  // Each tick the fd takes MORE bytes, so the stall counter keeps restarting
  // however deep the backlog gets -- a busy reader is not a stalled link.
  EXPECT_EQ(wd.tick("CONFIGURED", 60, SerialWatchdog::kReaderProofBytes + 1, true, 2000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 70, SerialWatchdog::kReaderProofBytes + 2, true, 3000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 80, SerialWatchdog::kReaderProofBytes + 3, true, 4000), Action::None);
  // It stops. Now the run counts from here.
  EXPECT_EQ(wd.tick("CONFIGURED", 90, SerialWatchdog::kReaderProofBytes + 3, true, 5000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 90, SerialWatchdog::kReaderProofBytes + 3, true, 6000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 90, SerialWatchdog::kReaderProofBytes + 3, true, 7000), Action::ReopenStalled);
}

TEST(SerialWatchdog, RetryBacksOffAfterRepeatedFailures) {
  SerialWatchdog wd;
  std::int64_t t = 0;
  // Drive several failed retries; each failure widens the next allowed interval.
  for (int i = 0; i < 6; ++i) {
    // Advance far enough that whatever the current interval is, a retry fires.
    t += SerialWatchdog::kMaxRetryMs;
    EXPECT_EQ(wd.tick("", 0, 0, false, t), Action::ReopenRetry);
    wd.on_reopen_result(/*succeeded=*/false);
  }
  EXPECT_GE(wd.consec_retry_failures(), SerialWatchdog::kBackoffStartAt);
}

TEST(SerialWatchdog, AGadgetBufferSwallowingWritesIsNotAReader) {
  // The second half of the same bug. `accepted` counts bytes the FD took, and a
  // CDC-ACM gadget with no host attached still takes a few KB into its own TX
  // buffer before it blocks — so every fresh link shows accepted climbing off
  // zero whether or not anyone is there. Believing that put the reopen cycle
  // back at one per ~12 s on GILABS-nB2UzE1S with nothing on /dev/ttyGS0.
  // Below the proof threshold, this must stay quiet forever.
  SerialWatchdog wd;
  const std::uint64_t buffered = SerialWatchdog::kReaderProofBytes - 1;
  for (int i = 0; i < 40; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 100 + i, buffered, true, i * 1000),
              Action::None)
        << "reopened at tick " << i << " on a gadget buffer, not a reader";
  }
}

TEST(SerialWatchdog, ThePreviousLinksTrafficDoesNotVouchForTheNextOne) {
  // `accepted` is monotonic across reopens, so the threshold must be measured
  // from each link's own start. Otherwise one genuine reader early in the boot
  // would certify every later link forever, and the stall path would re-arm on
  // a gadget nobody has touched since.
  SerialWatchdog wd;
  const std::uint64_t traffic = SerialWatchdog::kReaderProofBytes * 4;
  // A real reader, then the link goes stale and is reopened.
  EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, traffic, true, 1000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 110, traffic, true, 2000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 120, traffic, true, 3000), Action::None);
  ASSERT_EQ(wd.tick("CONFIGURED", 130, traffic, true, 4000),
            Action::ReopenStalled);
  // Fresh link: accepted has NOT moved since, so nobody is reading this one.
  for (int i = 4; i < 30; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 100 + i, traffic, true, i * 1000),
              Action::None)
        << "the old link's traffic vouched for the new one at tick " << i;
  }
}
