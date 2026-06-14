// Reconnect coverage for the active-object fd endpoints: FramedFdEndpoint's
// FdFactory / Tick reopen / backoff and SerialEndpoint wiring the SerialWatchdog
// to a real reopen via injected link factory + USB-state reader.
//
// The reopen/backoff/watchdog logic lives in the protected Tick()/Reopen()/
// MarkLinkDead(), driven on the endpoint's own I/O thread once Start()ed. These
// tests drive them DIRECTLY via a test subclass (no thread) so timing is
// deterministic and there is no data race on the internal link. The EOF→reopen
// wiring on the read path is exercised threaded by test_io (fixed-link on_closed).
// All host-runnable over socketpairs — no /dev/ttyGS0, no hardware.
#include "visio_schema/transport/framed_fd.hpp"

#include <gtest/gtest.h>
#include <sys/socket.h>
#include <unistd.h>

#include <memory>
#include <string>
#include <vector>

#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/serial.hpp"

using visio_schema::transport::FdFactory;
using visio_schema::transport::FramedFdEndpoint;
using visio_schema::transport::SerialEndpoint;
using visio_schema::transport::WritePolicy;

namespace {

// An FdFactory stand-in: each call makes a fresh socketpair and hands back one
// end's fd (the endpoint adopts it + sets O_NONBLOCK), retaining the peer fd so a
// test can close it to simulate the far side dropping. `return_fail` models a
// failed (re)connect (-1).
struct FakeFactory {
  int invocations = 0;
  bool return_fail = false;
  std::vector<int> peer_fds;

  int operator()() {
    ++invocations;
    if (return_fail) return -1;
    int sv[2] = {-1, -1};
    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) return -1;
    peer_fds.push_back(sv[1]);
    return sv[0];
  }
  int last_peer() const { return peer_fds.back(); }
};

FdFactory FnOf(std::shared_ptr<FakeFactory> s) {
  return [s] { return (*s)(); };
}

// Exposes the protected reopen hooks so tests can drive them synchronously,
// standing in for the I/O thread (which calls Tick() each loop and MarkLinkDead()
// on a read EOF).
class TestFd : public FramedFdEndpoint {
 public:
  using FramedFdEndpoint::FramedFdEndpoint;
  using FramedFdEndpoint::Tick;
  void SimulatePeerEof() { MarkLinkDead(); }
};

class TestSerial : public SerialEndpoint {
 public:
  using SerialEndpoint::SerialEndpoint;
  using SerialEndpoint::Tick;
  void SimulatePeerEof() { MarkLinkDead(); }
};

}  // namespace

TEST(EndpointReconnect, NullFactoryLeavesEndpointDown) {
  TestFd ep(FdFactory([] { return -1; }));
  EXPECT_FALSE(ep.link_up());
  ep.Tick(0);  // a failing factory stays down, no crash
  EXPECT_FALSE(ep.link_up());
}

TEST(EndpointReconnect, SelfHealsOnPeerCloseAndReopensOnTick) {
  auto s = std::make_shared<FakeFactory>();
  TestFd ep(FnOf(s), WritePolicy::drop_oldest(), /*reopen_backoff_ns=*/1000);
  ASSERT_TRUE(ep.link_up());  // factory ran eagerly in the ctor
  EXPECT_EQ(s->invocations, 1);

  ::close(s->last_peer());  // far side hangs up
  ep.SimulatePeerEof();     // what the I/O thread does when ReadSome reports EOF
  EXPECT_FALSE(ep.link_up());

  ep.Tick(0);  // MarkLinkDead armed an immediate retry
  EXPECT_TRUE(ep.link_up());
  EXPECT_EQ(s->invocations, 2);
}

TEST(EndpointReconnect, FailedReopenBacksOff) {
  auto s = std::make_shared<FakeFactory>();
  s->return_fail = true;  // every (re)connect fails
  TestFd ep(FnOf(s), WritePolicy::drop_oldest(), /*reopen_backoff_ns=*/1000);
  EXPECT_FALSE(ep.link_up());
  EXPECT_EQ(s->invocations, 1);  // initial attempt (in the ctor)

  ep.Tick(0);                    // retry → fails → arms backoff until t=1000
  EXPECT_EQ(s->invocations, 2);
  ep.Tick(500);                  // within backoff → no attempt
  EXPECT_EQ(s->invocations, 2);
  ep.Tick(1000);                 // backoff elapsed → retry
  EXPECT_EQ(s->invocations, 3);
}

TEST(EndpointReconnect, StopHaltsIoThreadWithoutReopen) {
  auto s = std::make_shared<FakeFactory>();
  FramedFdEndpoint ep(FnOf(s), WritePolicy::drop_oldest(), 1000);
  ASSERT_TRUE(ep.link_up());
  EXPECT_EQ(s->invocations, 1);
  ep.Start(nullptr, nullptr);
  ep.Stop();  // joins the I/O thread + closes the link
  EXPECT_FALSE(ep.link_up());
  // The link stayed up for the run, so Tick never reopened: factory ran once.
  EXPECT_EQ(s->invocations, 1);
}

// The fixed-fd SerialEndpoint ctor is the NON-RECONNECTING mode: no factory, no
// watchdog. After a peer EOF the link stays down and a Tick() must NOT reopen it
// (vs the watchdog/factory modes above) — the owner is expected to re-discover
// the device (whose /dev/ttyACMn number may have changed) rather than the
// endpoint silently reopening a stale path.
TEST(SerialEndpointNonReconnecting, FixedFdDoesNotReopenAfterEof) {
  int sv[2] = {-1, -1};
  ASSERT_EQ(::socketpair(AF_UNIX, SOCK_STREAM, 0, sv), 0);
  TestSerial ep(sv[0]);  // fixed-fd ctor: adopts the fd, no factory/watchdog
  EXPECT_TRUE(ep.link_up());

  ::close(sv[1]);         // far side hangs up
  ep.SimulatePeerEof();   // what the I/O thread does when ReadSome reports EOF
  EXPECT_FALSE(ep.link_up());

  ep.Tick(0);             // no factory → must stay down (no reconnect)
  EXPECT_FALSE(ep.link_up());
}

TEST(SerialEndpointWatchdog, ReopensOnConfiguredEdge) {
  auto s = std::make_shared<FakeFactory>();
  std::string usb = "DISCONNECTED";
  TestSerial ep(FnOf(s), WritePolicy::drop_oldest(), [&usb] { return usb; });
  ASSERT_TRUE(ep.link_up());
  EXPECT_EQ(s->invocations, 1);

  ep.Tick(0);  // seeds prev USB state; healthy + draining → no reopen
  EXPECT_EQ(s->invocations, 1);

  usb = "CONFIGURED";        // host (re)enumerated → CONFIGURED edge
  ep.Tick(1'000'000'000);    // now_ms = 1000
  EXPECT_EQ(s->invocations, 2);  // watchdog forced a reopen
  EXPECT_TRUE(ep.link_up());
}
