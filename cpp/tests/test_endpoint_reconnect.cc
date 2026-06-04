// Reconnect coverage for the reactor endpoints: FramedFdEndpoint's LinkFactory /
// OnTick reopen / backoff / read-path self-heal, and SerialEndpoint wiring the
// SerialWatchdog to a real reopen via injected link factory + USB-state reader.
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

using visio_schema::transport::FramedFdEndpoint;
using visio_schema::transport::Link;
using visio_schema::transport::MakeFdLink;
using visio_schema::transport::SerialEndpoint;
using visio_schema::transport::WritePolicy;

namespace {

// A LinkFactory stand-in: each call makes a fresh socketpair and hands back a
// non-blocking Link for one end, retaining the peer fd so a test can close it to
// simulate the far side dropping. `return_null` models a failed (re)connect.
struct FakeFactory {
  int invocations = 0;
  bool return_null = false;
  std::vector<int> peer_fds;

  std::shared_ptr<Link> operator()() {
    ++invocations;
    if (return_null) return nullptr;
    int sv[2] = {-1, -1};
    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) return nullptr;
    peer_fds.push_back(sv[1]);
    return MakeFdLink(sv[0], /*set_raw=*/false, /*write_timeout_ms=*/0,
                      /*nonblocking=*/true);
  }
  int last_peer() const { return peer_fds.back(); }
};

FramedFdEndpoint::LinkFactory FnOf(std::shared_ptr<FakeFactory> s) {
  return [s] { return (*s)(); };
}

}  // namespace

TEST(EndpointReconnect, NullFactoryLeavesEndpointDown) {
  FramedFdEndpoint ep(FramedFdEndpoint::LinkFactory([] {
    return std::shared_ptr<Link>();
  }));
  EXPECT_FALSE(ep.link_up());
  EXPECT_EQ(ep.Fileno(), -1);
  EXPECT_EQ(ep.PollEvents(), 0);  // down → bus relies on the tick, not poll
}

TEST(EndpointReconnect, SelfHealsOnPeerCloseAndReopensOnTick) {
  auto s = std::make_shared<FakeFactory>();
  FramedFdEndpoint ep(FnOf(s), WritePolicy::drop_oldest(),
                      /*reopen_backoff_ns=*/1000);
  ASSERT_TRUE(ep.link_up());
  EXPECT_EQ(s->invocations, 1);

  ::close(s->last_peer());  // far side hangs up
  // A reopenable endpoint surfaces EOF as a silent self-heal, NOT a throw.
  EXPECT_NO_THROW({ (void)ep.TryRead(); });
  EXPECT_FALSE(ep.link_up());

  ep.OnTick(0);  // MarkLinkDead armed an immediate retry
  EXPECT_TRUE(ep.link_up());
  EXPECT_EQ(s->invocations, 2);
}

TEST(EndpointReconnect, FailedReopenBacksOff) {
  auto s = std::make_shared<FakeFactory>();
  s->return_null = true;  // every (re)connect fails
  FramedFdEndpoint ep(FnOf(s), WritePolicy::drop_oldest(),
                      /*reopen_backoff_ns=*/1000);
  EXPECT_FALSE(ep.link_up());
  EXPECT_EQ(s->invocations, 1);  // initial attempt

  ep.OnTick(0);                  // retry → fails → arms backoff until t=1000
  EXPECT_EQ(s->invocations, 2);
  ep.OnTick(500);               // within backoff → no attempt
  EXPECT_EQ(s->invocations, 2);
  ep.OnTick(1000);              // backoff elapsed → retry
  EXPECT_EQ(s->invocations, 3);
}

TEST(EndpointReconnect, CloseStopsReconnect) {
  auto s = std::make_shared<FakeFactory>();
  FramedFdEndpoint ep(FnOf(s), WritePolicy::drop_oldest(), 1000);
  ASSERT_TRUE(ep.link_up());
  ep.Close();
  EXPECT_FALSE(ep.link_up());
  ep.OnTick(0);
  ep.OnTick(1'000'000);
  EXPECT_EQ(s->invocations, 1);  // factory never called again after Close()
}

TEST(SerialEndpointWatchdog, ReopensOnConfiguredEdge) {
  auto s = std::make_shared<FakeFactory>();
  std::string usb = "DISCONNECTED";
  SerialEndpoint ep(FnOf(s), WritePolicy::drop_oldest(),
                    [&usb] { return usb; });
  ASSERT_TRUE(ep.link_up());
  EXPECT_EQ(s->invocations, 1);

  ep.OnTick(0);  // seeds prev USB state; healthy + draining → no reopen
  EXPECT_EQ(s->invocations, 1);

  usb = "CONFIGURED";                 // host (re)enumerated → CONFIGURED edge
  ep.OnTick(1'000'000'000);           // now_ms = 1000
  EXPECT_EQ(s->invocations, 2);       // watchdog forced a reopen
  EXPECT_TRUE(ep.link_up());
}
