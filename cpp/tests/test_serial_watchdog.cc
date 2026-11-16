// SerialWatchdog decisions with injected USB, reader and transport state.
#include "visio_schema/transport/serial_watchdog.hpp"

#include <gtest/gtest.h>

using visio_schema::transport::SerialWatchdog;
using Action = visio_schema::transport::SerialWatchdog::Action;

TEST(SerialWatchdog, RetriesWhileLinkDown) {
  SerialWatchdog wd;
  EXPECT_EQ(wd.tick("", 0, 0, false, false, 0), Action::ReopenRetry);
}

TEST(SerialWatchdog, RetryIsRateLimited) {
  SerialWatchdog wd;
  EXPECT_EQ(wd.tick("", 0, 0, false, false, 0), Action::ReopenRetry);
  EXPECT_EQ(wd.tick("", 0, 0, false, false, 5000), Action::None);
  EXPECT_EQ(wd.tick("", 0, 0, false, false, 11000), Action::ReopenRetry);
}

TEST(SerialWatchdog, HealthyDrainingLinkIsQuiet) {
  SerialWatchdog wd;
  for (int i = 0; i < 5; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 0, 0, true, false, i * 1000),
              Action::None);
  }
}

TEST(SerialWatchdog, ReopensOnConfiguredEdge) {
  SerialWatchdog wd;
  EXPECT_EQ(wd.tick("CONNECTED", 0, 0, true, false, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 0, 0, true, false, 1000),
            Action::ReopenEdge);
}

TEST(SerialWatchdog, ReopensOnDrainStall) {
  SerialWatchdog wd;
  const auto seen = SerialWatchdog::kReaderProofBytes;
  EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, false, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 20, seen, true, false, 1000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, seen, true, false, 2000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, seen, true, false, 3000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, seen, true, true, 4000),
            Action::ReopenStalled);
}

TEST(SerialWatchdog, NoReaderDoesNotReopenOnStall) {
  SerialWatchdog wd;
  // No host has read this link, even though the transport is stalled.
  for (int i = 0; i < 10; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, true, i * 1000),
              Action::None);
  }
}

TEST(SerialWatchdog, OutboxEvictingItsOwnBacklogIsNotAReader) {
  SerialWatchdog wd;
  for (int i = 0; i < 40; ++i) {
    // A bounded queue fills and evicts; no bytes reach the fd.
    const std::size_t pending = (i % 4 == 3) ? 20 : 100 + i;
    EXPECT_EQ(wd.tick("CONFIGURED", pending, 0, true, true, i * 1000),
              Action::None);
  }
}

TEST(SerialWatchdog, AReaderThatStopsStillStallsWhileTheOutboxEvicts) {
  SerialWatchdog wd;
  const auto seen = SerialWatchdog::kReaderProofBytes;
  EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, false, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, seen, true, false, 1000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 20, seen, true, false, 2000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, seen, true, false, 3000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 30, seen, true, true, 4000),
            Action::ReopenStalled);
}

TEST(SerialWatchdog, ProgressKeepsLinkHealthy) {
  SerialWatchdog wd;
  const auto seen = SerialWatchdog::kReaderProofBytes;
  EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, false, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 50, seen, true, false, 1000), Action::None);
  for (int i = 1; i <= 3; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 50 + i, seen + i, true, false,
                      1000 + i * 1000), Action::None);
  }
  EXPECT_EQ(wd.tick("CONFIGURED", 90, seen + 3, true, false, 5000),
            Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 90, seen + 3, true, false, 6000),
            Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 90, seen + 3, true, true, 7000),
            Action::ReopenStalled);
}

TEST(SerialWatchdog, RetryBacksOffAfterRepeatedFailures) {
  SerialWatchdog wd;
  std::int64_t t = 0;
  for (int i = 0; i < 6; ++i) {
    t += SerialWatchdog::kMaxRetryMs;
    EXPECT_EQ(wd.tick("", 0, 0, false, false, t), Action::ReopenRetry);
    wd.on_reopen_result(false);
  }
  EXPECT_GE(wd.consec_retry_failures(), SerialWatchdog::kBackoffStartAt);
}

TEST(SerialWatchdog, AGadgetBufferSwallowingWritesIsNotAReader) {
  SerialWatchdog wd;
  const auto buffered = SerialWatchdog::kReaderProofBytes - 1;
  for (int i = 0; i < 40; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 100 + i, buffered, true, true, i * 1000),
              Action::None);
  }
}

TEST(SerialWatchdog, ThePreviousLinksTrafficDoesNotVouchForTheNextOne) {
  SerialWatchdog wd;
  const auto traffic = SerialWatchdog::kReaderProofBytes * 4;
  EXPECT_EQ(wd.tick("CONFIGURED", 100, 0, true, false, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, traffic, true, false, 1000),
            Action::None);
  ASSERT_EQ(wd.tick("CONFIGURED", 130, traffic, true, true, 4000),
            Action::ReopenStalled);
  for (int i = 4; i < 30; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 100 + i, traffic, true, true, i * 1000),
              Action::None);
  }
}

TEST(SerialWatchdog, KeepsBriefBackpressure) {
  SerialWatchdog watchdog;
  const auto seen = SerialWatchdog::kReaderProofBytes;
  ASSERT_EQ(watchdog.tick("CONFIGURED", 1024, seen, true, false, 0),
            Action::None);
  for (int elapsed_ms = 1; elapsed_ms <= 10; ++elapsed_ms) {
    EXPECT_EQ(watchdog.tick("CONFIGURED", 1024, seen, true, false, elapsed_ms),
              Action::None)
        << "active link closed after " << elapsed_ms << " ms";
  }
}

TEST(SerialWatchdog, StallNeedsPendingConfiguredLink) {
  SerialWatchdog wd;
  const auto seen = SerialWatchdog::kReaderProofBytes;
  EXPECT_EQ(wd.tick("CONFIGURED", 100, seen, true, false, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 0, seen, true, true, 4000), Action::None);
  EXPECT_EQ(wd.tick("CONNECTED", 100, seen, true, true, 5000), Action::None);
  // Empty USB state is no signal; a stale stall latch cannot reopen by itself.
  EXPECT_EQ(wd.tick("", 100, seen, true, true, 6000), Action::None);
}
