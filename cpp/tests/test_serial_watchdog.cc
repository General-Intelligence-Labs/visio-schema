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
  EXPECT_EQ(wd.tick("", 0, /*client_open=*/false, 0), Action::ReopenRetry);
}

TEST(SerialWatchdog, RetryIsRateLimited) {
  SerialWatchdog wd;
  EXPECT_EQ(wd.tick("", 0, false, 0), Action::ReopenRetry);
  // A second retry within kBaseRetryMs (10 s) is suppressed.
  EXPECT_EQ(wd.tick("", 0, false, 5000), Action::None);
  // Past the interval it fires again.
  EXPECT_EQ(wd.tick("", 0, false, 11000), Action::ReopenRetry);
}

TEST(SerialWatchdog, HealthyDrainingLinkIsQuiet) {
  SerialWatchdog wd;
  for (int i = 0; i < 5; ++i) {
    EXPECT_EQ(wd.tick("CONFIGURED", 0, /*client_open=*/true, i * 1000),
              Action::None);
  }
}

TEST(SerialWatchdog, ReopensOnConfiguredEdge) {
  SerialWatchdog wd;
  EXPECT_EQ(wd.tick("CONNECTED", 0, true, 0), Action::None);  // seeds prev state
  EXPECT_EQ(wd.tick("CONFIGURED", 0, true, 1000), Action::ReopenEdge);
}

TEST(SerialWatchdog, ReopensOnDrainStall) {
  SerialWatchdog wd;
  // Pending bytes stuck (non-decreasing) for kStallTicks while CONFIGURED.
  EXPECT_EQ(wd.tick("CONFIGURED", 100, true, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, true, 1000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 100, true, 2000), Action::ReopenStalled);
}

TEST(SerialWatchdog, DrainingResetsStallCounter) {
  SerialWatchdog wd;
  EXPECT_EQ(wd.tick("CONFIGURED", 100, true, 0), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 50, true, 1000), Action::None);   // pending fell → draining
  EXPECT_EQ(wd.tick("CONFIGURED", 60, true, 2000), Action::None);   // counter restarted
  EXPECT_EQ(wd.tick("CONFIGURED", 60, true, 3000), Action::None);
  EXPECT_EQ(wd.tick("CONFIGURED", 60, true, 4000), Action::ReopenStalled);
}

TEST(SerialWatchdog, RetryBacksOffAfterRepeatedFailures) {
  SerialWatchdog wd;
  std::int64_t t = 0;
  // Drive several failed retries; each failure widens the next allowed interval.
  for (int i = 0; i < 6; ++i) {
    // Advance far enough that whatever the current interval is, a retry fires.
    t += SerialWatchdog::kMaxRetryMs;
    EXPECT_EQ(wd.tick("", 0, false, t), Action::ReopenRetry);
    wd.on_reopen_result(/*succeeded=*/false);
  }
  EXPECT_GE(wd.consec_retry_failures(), SerialWatchdog::kBackoffStartAt);
}
