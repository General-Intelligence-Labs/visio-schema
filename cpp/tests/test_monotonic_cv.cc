// MonotonicConditionVariable — the waits keep std::condition_variable's
// contract: a timeout returns after the timeout and reports it, a notify
// wakes a waiter early, a predicate is re-checked across both, and a
// "forever" timeout neither overflows nor times out at once. That a wall
// clock step leaves them alone needs root to show, so it is a board check.
#include <gtest/gtest.h>

#include <chrono>
#include <mutex>
#include <thread>

#include "visio_schema/transport/monotonic_cv.hpp"

using visio_schema::transport::MonotonicConditionVariable;
using Clock = std::chrono::steady_clock;
using std::chrono::milliseconds;
using std::chrono::seconds;

TEST(MonotonicConditionVariable, ATimeoutReturnsAfterItAndSaysSo) {
  MonotonicConditionVariable cv;
  std::mutex mu;
  std::unique_lock<std::mutex> lk(mu);
  const auto t0 = Clock::now();
  EXPECT_EQ(cv.wait_for(lk, milliseconds(50)), std::cv_status::timeout);
  EXPECT_GE(Clock::now() - t0, milliseconds(50));
  EXPECT_FALSE(cv.wait_for(lk, milliseconds(20), [] { return false; }));
}

TEST(MonotonicConditionVariable, ANotifyWakesTheWaiterWithItsPredicate) {
  MonotonicConditionVariable cv;
  std::mutex mu;
  bool ready = false;
  std::thread setter([&] {
    std::this_thread::sleep_for(milliseconds(20));
    {
      std::lock_guard<std::mutex> g(mu);
      ready = true;
    }
    cv.notify_one();
  });
  std::unique_lock<std::mutex> lk(mu);
  const auto t0 = Clock::now();
  EXPECT_TRUE(cv.wait_for(lk, seconds(10), [&] { return ready; }));
  EXPECT_LT(Clock::now() - t0, seconds(5));
  lk.unlock();
  setter.join();
}

TEST(MonotonicConditionVariable, AnAlreadyTruePredicateDoesNotWait) {
  MonotonicConditionVariable cv;
  std::mutex mu;
  std::unique_lock<std::mutex> lk(mu);
  const auto t0 = Clock::now();
  EXPECT_TRUE(cv.wait_until(lk, Clock::now() + seconds(10),
                            [] { return true; }));
  EXPECT_LT(Clock::now() - t0, milliseconds(100));
}

TEST(MonotonicConditionVariable, AForeverTimeoutWaitsRatherThanOverflows) {
  MonotonicConditionVariable cv;
  std::mutex mu;
  bool ready = false;
  std::thread setter([&] {
    std::this_thread::sleep_for(milliseconds(50));
    {
      std::lock_guard<std::mutex> g(mu);
      ready = true;
    }
    cv.notify_one();
  });
  std::unique_lock<std::mutex> lk(mu);
  const auto t0 = Clock::now();
  EXPECT_TRUE(cv.wait_for(lk, std::chrono::hours::max(),
                          [&] { return ready; }));
  EXPECT_GE(Clock::now() - t0, milliseconds(40));
  lk.unlock();
  setter.join();
}
