// WakeFd tests — the pollable cross-thread wakeup. These directly pin Signal/
// Drain semantics on BOTH backends (eventfd on Linux, self-pipe on macOS/BSD),
// which is the only place that catches a broken wake: the endpoint loops poll
// with a 200 ms tick, so a no-op Signal would otherwise be masked.
#include <gtest/gtest.h>

#include <poll.h>

#include "visio_schema/transport/wake_fd.hpp"

using visio_schema::transport::WakeFd;

namespace {
bool Readable(const WakeFd& w, int timeout_ms) {
  pollfd p{w.poll_fd(), POLLIN, 0};
  ::poll(&p, 1, timeout_ms);
  return (p.revents & POLLIN) != 0;
}
}  // namespace

TEST(WakeFd, SignalWakesPollAndDrainClears) {
  WakeFd w;
  ASSERT_TRUE(w.Open());
  EXPECT_FALSE(Readable(w, 10));   // idle: poll times out
  w.Signal();
  EXPECT_TRUE(Readable(w, 100));   // signalled: readable promptly
  EXPECT_TRUE(Readable(w, 10));    // level-triggered: stays readable until drained
  w.Drain();
  EXPECT_FALSE(Readable(w, 10));   // drained: times out again
}

TEST(WakeFd, RepeatedSignalCoalescesIntoOneDrain) {
  WakeFd w;
  ASSERT_TRUE(w.Open());
  for (int i = 0; i < 5; ++i) w.Signal();
  EXPECT_TRUE(Readable(w, 100));
  w.Drain();                       // a single drain clears all pending wakeups
  EXPECT_FALSE(Readable(w, 10));
}

TEST(WakeFd, CloseIsIdempotentAndReopenable) {
  WakeFd w;
  ASSERT_TRUE(w.Open());
  EXPECT_TRUE(w.is_open());
  w.Close();
  EXPECT_FALSE(w.is_open());
  w.Close();                       // idempotent
  ASSERT_TRUE(w.Open());           // reopen works
  w.Signal();
  EXPECT_TRUE(Readable(w, 100));
}

TEST(WakeFd, SignalBeforeOpenIsNoOp) {
  WakeFd w;
  w.Signal();                      // no fd yet — must not crash
  EXPECT_FALSE(w.is_open());
}
