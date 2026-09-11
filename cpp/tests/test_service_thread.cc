// EnterServiceThread — every library loop enters through it, so the two
// things it promises are pinned here: the calling thread ends up named and
// timeshared at the requested nice, and a thread BORN under a real-time
// policy (inherited from its creator) is put back on SCHED_OTHER. The second
// is the Ego Pro head bug: the hub discovery thread had been swept onto
// SCHED_FIFO, and every limb link it opened inherited that.
#include <gtest/gtest.h>
#include <pthread.h>
#include <sched.h>
#if defined(__linux__)
#include <sys/prctl.h>
#include <sys/resource.h>
#endif

#include <thread>

#include "visio_schema/transport/link.hpp"

using visio_schema::transport::EnterServiceThread;

namespace {

int CurrentPolicy() {
  int policy = -1;
  sched_param p{};
  ::pthread_getschedparam(::pthread_self(), &policy, &p);
  return policy;
}

}  // namespace

TEST(ServiceThread, NamesAndTimesharesTheCallingThread) {
  std::thread t([] {
    EnterServiceThread("vs_test_svc", 7);
    EXPECT_EQ(CurrentPolicy(), SCHED_OTHER);
#if defined(__linux__)
    char comm[32] = {0};
    ::prctl(PR_GET_NAME, comm, 0, 0, 0);
    EXPECT_STREQ(comm, "vs_test_svc");
    EXPECT_EQ(::getpriority(PRIO_PROCESS, 0), 7);
#endif
  });
  t.join();
}

// Needs CAP_SYS_NICE to stage the real-time parent, so it self-skips where
// the suite runs unprivileged (CI); on a dev box run it as root once.
TEST(ServiceThread, AThreadBornUnderSchedFifoLeavesIt) {
  sched_param rt{};
  rt.sched_priority = 30;
  if (::pthread_setschedparam(::pthread_self(), SCHED_FIFO, &rt) != 0) {
    GTEST_SKIP() << "needs CAP_SYS_NICE to put the parent on SCHED_FIFO";
  }
  int before = -1, after = -1;
  std::thread t([&] {
    before = CurrentPolicy();  // inherited from the FIFO parent
    EnterServiceThread("vs_test_child", 5);
    after = CurrentPolicy();
  });
  t.join();
  sched_param timeshared{};
  ::pthread_setschedparam(::pthread_self(), SCHED_OTHER, &timeshared);

  EXPECT_EQ(before, SCHED_FIFO) << "the child did not inherit FIFO — the "
                                   "test proves nothing on this libc";
  EXPECT_EQ(after, SCHED_OTHER);
}
