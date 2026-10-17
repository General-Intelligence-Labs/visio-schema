// MonotonicConditionVariable — a condition variable whose timed waits are
// measured on the monotonic clock, never the wall clock.
//
// std::condition_variable::wait_for turns its timeout into a CLOCK_REALTIME
// deadline wherever the C library offers no monotonic clockwait: libstdc++
// before GCC 10 always (GCC bug 41861), and any libstdc++ over a libc
// without pthread_cond_clockwait, uClibc-ng included. Every timed waiter
// then sleeps for as long as a backward step of the wall clock — a phone
// setting the time on connect, an NTP correction — and a timer thread, a
// reconnect loop or a polling worker sleeps for hours instead of
// milliseconds. Waits here use CLOCK_MONOTONIC (Linux) or a relative wait
// (macOS), neither of which a clock step moves.
//
// The interface is the subset of std::condition_variable this library and
// its consumers call, so a member swaps in without touching its call sites.
#pragma once

#include <pthread.h>
#include <time.h>

#include <cerrno>
#include <chrono>
#include <condition_variable>  // std::cv_status
#include <cstdint>
#include <cstdlib>
#include <mutex>

namespace visio_schema::transport {

class MonotonicConditionVariable {
 public:
  using Clock = std::chrono::steady_clock;

  MonotonicConditionVariable() {
    pthread_condattr_t attr;
    ::pthread_condattr_init(&attr);
#if !defined(__APPLE__)
    // Fails only for a clock the C library cannot wait on; a cond left on
    // the wall clock would turn every monotonic deadline below into one
    // long past, and every timed wait into a spin. Loud, not silent.
    if (::pthread_condattr_setclock(&attr, CLOCK_MONOTONIC) != 0)
      std::abort();
#endif
    if (::pthread_cond_init(&cond_, &attr) != 0) std::abort();
    ::pthread_condattr_destroy(&attr);
  }
  ~MonotonicConditionVariable() { ::pthread_cond_destroy(&cond_); }
  MonotonicConditionVariable(const MonotonicConditionVariable&) = delete;
  MonotonicConditionVariable& operator=(const MonotonicConditionVariable&) =
      delete;

  void notify_one() noexcept { ::pthread_cond_signal(&cond_); }
  void notify_all() noexcept { ::pthread_cond_broadcast(&cond_); }

  void wait(std::unique_lock<std::mutex>& lk) {
    if (::pthread_cond_wait(&cond_, lk.mutex()->native_handle()) != 0)
      std::abort();
  }
  template <class Predicate>
  void wait(std::unique_lock<std::mutex>& lk, Predicate ready) {
    while (!ready()) wait(lk);
  }

  // `deadline` is a steady_clock point: the only clock this waits on.
  std::cv_status wait_until(std::unique_lock<std::mutex>& lk,
                            Clock::time_point deadline) {
    return TimedWait(lk, deadline) ? std::cv_status::no_timeout
                                   : std::cv_status::timeout;
  }
  template <class Predicate>
  bool wait_until(std::unique_lock<std::mutex>& lk,
                  Clock::time_point deadline, Predicate ready) {
    while (!ready()) {
      if (!TimedWait(lk, deadline)) return ready();
    }
    return true;
  }

  template <class Rep, class Period>
  std::cv_status wait_for(std::unique_lock<std::mutex>& lk,
                          const std::chrono::duration<Rep, Period>& timeout) {
    return wait_until(lk, DeadlineAfter(timeout));
  }
  template <class Rep, class Period, class Predicate>
  bool wait_for(std::unique_lock<std::mutex>& lk,
                const std::chrono::duration<Rep, Period>& timeout,
                Predicate ready) {
    return wait_until(lk, DeadlineAfter(timeout), ready);
  }

 private:
  // Past any real wait, short of steady_clock's range: a caller asking
  // for "forever" (duration::max()) must not overflow into the past.
  static constexpr std::chrono::hours kLongestWait{24 * 365};
  static constexpr std::int64_t kNsPerSecond = 1000000000;

  template <class Rep, class Period>
  static Clock::time_point DeadlineAfter(
      const std::chrono::duration<Rep, Period>& timeout) {
    if (timeout > kLongestWait) return Clock::now() + kLongestWait;
    return Clock::now() +
           std::chrono::duration_cast<Clock::duration>(timeout);
  }

  // A timed wait's result: woken (true), timed out (false). Anything else
  // is a misuse (EINVAL, EPERM) that, read as a wake, would spin a
  // predicate loop hot until its deadline — loud instead.
  static bool Woke(int rc) {
    if (rc == 0) return true;
    if (rc != ETIMEDOUT) std::abort();
    return false;
  }

  // One wait toward `deadline`; false once it has passed. A wake before it
  // (a notify, or a spurious one) returns true, and the caller re-checks.
  bool TimedWait(std::unique_lock<std::mutex>& lk,
                 Clock::time_point deadline) {
    using std::chrono::nanoseconds;
    const auto left = deadline - Clock::now();
    if (left <= Clock::duration::zero()) return false;
#if defined(__APPLE__)
    const std::int64_t ns =
        std::chrono::duration_cast<nanoseconds>(left).count();
    const timespec rel{static_cast<time_t>(ns / kNsPerSecond),
                       static_cast<long>(ns % kNsPerSecond)};
    return Woke(::pthread_cond_timedwait_relative_np(
        &cond_, lk.mutex()->native_handle(), &rel));
#else
    // steady_clock IS CLOCK_MONOTONIC here (libstdc++ and libc++ on Linux;
    // wire/time.hpp's MonotonicNs relies on the same), so the deadline
    // converts directly into the cond's clock.
    const std::int64_t ns = std::chrono::duration_cast<nanoseconds>(
                                deadline.time_since_epoch()).count();
    const timespec at{static_cast<time_t>(ns / kNsPerSecond),
                      static_cast<long>(ns % kNsPerSecond)};
    return Woke(::pthread_cond_timedwait(&cond_, lk.mutex()->native_handle(),
                                         &at));
#endif
  }

  pthread_cond_t cond_;
};

}  // namespace visio_schema::transport
