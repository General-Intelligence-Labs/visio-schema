// Backpressure / stalled-peer — an active-object endpoint owns a bounded outbox
// with a WritePolicy. A stalled peer (one that never reads) can't drain the fd,
// so the outbox fills and SHEDS frames (dropped() rises) while Send() stays
// non-blocking on the caller's thread — it never blocks behind a slow consumer.
// The positive control shows a draining peer sheds nothing.
#include <gtest/gtest.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <functional>
#include <mutex>
#include <string>
#include <utility>
#include <vector>
#include <thread>

#include "visio_schema/transport/framed_fd.hpp"
#include "visio_schema/transport/framing.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/write_policy.hpp"
#include "visio_schema/wire/control.hpp"

using namespace std::chrono_literals;
using visio_schema::transport::CloseFd;
using visio_schema::transport::ExtractFrames;
using visio_schema::transport::FramedFdEndpoint;
using visio_schema::transport::MakeFdPair;
using visio_schema::transport::ReadSome;
using visio_schema::transport::WritePolicy;
using visio_schema::wire::Message;

namespace {

// Shrink both ends' socket buffers so a non-draining peer fills fast and the
// outbox starts shedding within a small flood.
void ShrinkBuffers(int a, int b) {
  int sz = 4096;
  ::setsockopt(a, SOL_SOCKET, SO_SNDBUF, &sz, sizeof(sz));
  ::setsockopt(b, SOL_SOCKET, SO_RCVBUF, &sz, sizeof(sz));
}

// Mark the frame `bulk` so Send() routes it to the outbox whose depth THIS
// endpoint's WritePolicy configures (`outbox_`). Non-bulk frames go to the
// separate control outbox, which is hardwired to drop_oldest(512) and ignores
// the constructor policy — so a non-bulk flood tests a queue the test never
// sized, making the shed/no-shed outcome a race (flaky, esp. on macOS where
// small default AF_UNIX socket buffers stall the drainer).
Message Frame(std::size_t n) {
  Message m;
  m.stream_id = visio_schema::kFirstDynamic;
  m.payload = std::string(n, 'x');
  m.bulk = true;
  return m;
}

// Poll until the endpoint has handed everything to the kernel (pending empty).
bool WaitUntilDrained(FramedFdEndpoint& tx, int max_ms = 3000) {
  for (int i = 0; i < max_ms; ++i) {
    if (tx.pending_bytes() == 0) return true;
    std::this_thread::sleep_for(1ms);
  }
  return tx.pending_bytes() == 0;
}

// Prime an unread peer with a bulk burst, then keep sending until `latched`
// reports true (or the attempt budget runs out). Each Send both evicts
// synchronously (byte cap, at enqueue) and Wake()s the I/O thread, which is
// what re-runs the stall clock and re-samples the eviction counter — without
// the re-sends a latch waits for the 200 ms poll tick. Returns latched().
bool FloodUntilLatched(FramedFdEndpoint& tx,
                       const std::function<bool()>& latched,
                       int attempts = 2000) {
  const Message flood = Frame(2048);
  for (int i = 0; i < 64; ++i) tx.Send(flood);
  for (int i = 0; i < attempts && !latched(); ++i) {
    tx.Send(flood);
    std::this_thread::sleep_for(1ms);
  }
  return latched();
}

// Background thread draining `fd` into an accumulating buffer — the "reader
// keeps up / comes back" side of these tests. `read_chunk` bounds each read()
// so a test can keep the drain slow relative to its flood.
class DrainingReader {
 public:
  explicit DrainingReader(int fd, std::size_t read_chunk = 4096)
      : chunk_(read_chunk), thread_([this, fd] { Run(fd); }) {}
  ~DrainingReader() { Stop(); }

  void Stop() {
    stop_.store(true);
    if (thread_.joinable()) thread_.join();
  }
  // Steal everything received so far. The caller keeps its own carry buffer,
  // so a frame split across two Take()s still reassembles.
  std::vector<std::uint8_t> Take() {
    std::lock_guard<std::mutex> lk(mu_);
    return std::exchange(rx_, {});
  }

 private:
  void Run(int fd) {
    std::vector<std::uint8_t> buf(chunk_);
    while (!stop_.load()) {
      const long n = ReadSome(fd, buf.data(), buf.size());
      if (n < 0) break;
      if (n == 0) {
        std::this_thread::sleep_for(1ms);
        continue;
      }
      std::lock_guard<std::mutex> lk(mu_);
      rx_.insert(rx_.end(), buf.begin(), buf.begin() + n);
    }
  }
  std::size_t chunk_;
  std::atomic<bool> stop_{false};
  std::mutex mu_;
  std::vector<std::uint8_t> rx_;
  std::thread thread_;  // last member: starts after everything it touches
};

}  // namespace

TEST(Backpressure, StalledPeerShedsAndNeverBlocks) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);

  // Small bounded (bulk) outbox; the peer `b` is never read, so the I/O thread
  // cannot drain `a` once the kernel buffer fills.
  FramedFdEndpoint tx(a, WritePolicy::drop_oldest(/*depth=*/8));
  tx.Start({}, {});  // write-only sink: no inbound / on_closed needed

  // Flood. Send() must never block the caller even though nothing drains.
  const Message m = Frame(512);
  for (int i = 0; i < 5000; ++i) tx.Send(m);

  // Shedding is applied at enqueue once the queue hits max_depth; give the I/O
  // thread a beat to fill the kernel buffer + back up the queue, then assert.
  bool shed = false;
  for (int i = 0; i < 200 && !shed; ++i) {
    if (tx.dropped() > 0) {
      shed = true;
    } else {
      tx.Send(m);
      std::this_thread::sleep_for(1ms);
    }
  }
  EXPECT_TRUE(shed) << "a stalled peer should make the bounded outbox shed frames";
  // Shedding must be substantial — most of the 5000-frame flood, since the
  // depth-8 outbox can only hold a handful. A regression that left the queue
  // effectively unbounded (the bug a bounded outbox prevents) would shed ~none.
  EXPECT_GT(tx.dropped(), 100u);

  tx.Stop();
  CloseFd(b);
}

TEST(Backpressure, DrainingPeerShedsNothing) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);

  std::atomic<bool> stop{false};
  std::thread reader([b = b, &stop] {
    std::uint8_t buf[4096];
    while (!stop.load()) {
      long n = ReadSome(b, buf, sizeof(buf));
      if (n < 0) break;            // EOF / dead
      if (n == 0) std::this_thread::sleep_for(1ms);  // would-block: nothing yet
    }
  });

  // Depth-1024 (bulk) outbox holds the whole 1000-frame flood without shedding,
  // even if the drainer never runs — so "sheds nothing" is deterministic, not a
  // race against the peer/OS.
  FramedFdEndpoint tx(a, WritePolicy::drop_oldest(1024));
  tx.Start({}, {});
  const Message m = Frame(512);
  for (int i = 0; i < 1000; ++i) tx.Send(m);

  // With the peer keeping up, the outbox drains to empty and nothing is shed.
  EXPECT_TRUE(WaitUntilDrained(tx, 500))
      << "outbox should fully drain to a keeping-up peer";
  EXPECT_EQ(tx.dropped(), 0u);

  tx.Stop();
  stop.store(true);
  CloseFd(b);
  reader.join();
}

// A draining-but-slow peer forces WriteSome to return EAGAIN, which is the case
// that used to throttle the leg: Pump() called Drain() once per poll wakeup and
// a OneAtATime outbox promotes exactly ONE frame per Drain, so the endpoint could
// only put one frame on the wire per POLLOUT. That is invisible on a fast link
// (writes never block, so poll returns immediately and the loop spins) but caps
// throughput on a real one — on the ego it left the kernel send queue empty while
// the viewer starved and its decoder lost sync. Pump() now keeps draining until
// the link stalls.
//
// The throughput half of that is NOT assertable here: over a socketpair poll()
// reports writable immediately, so even one-frame-per-wakeup keeps up and this
// test passes with or without the fix. It needs POLLOUT to be rate-limited by a
// real netdev, so it is verified on hardware instead (the app-side [visio-fps]
// meter: rx and rn both ~30/s, feedGapMax near one frame interval).
//
// Pinned here by the two properties that can actually break: EVERYTHING sent
// arrives, and it arrives INTACT — the drain loop re-picks between the control
// and bulk outboxes each iteration, so a boundary bug would splice two frames
// together and desync the reader.
TEST(Backpressure, DrainsBurstThroughAStallingLinkWithoutSplicingFrames) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);  // small buffers => writes hit EAGAIN mid-burst

  DrainingReader reader(b, /*read_chunk=*/1024);  // slow drain vs the burst

  // OneAtATime is the mode the device's TCP leg uses; the queue is deep enough
  // that nothing sheds, so a missing frame means it was never drained.
  WritePolicy policy = WritePolicy::drop_oldest(4096);
  policy.drain = WritePolicy::DrainMode::OneAtATime;
  FramedFdEndpoint tx(a, policy);
  tx.Start({}, {});

  constexpr int kFrames = 300;
  for (int i = 0; i < kFrames; ++i) {
    Message bulk = Frame(512);
    bulk.seq = static_cast<std::uint32_t>(i);
    tx.Send(bulk);
    if (i % 10 == 0) {  // interleave control traffic across the drain loop
      Message ctrl;
      ctrl.stream_id = visio_schema::kCommand;
      ctrl.payload = "ctrl";
      ctrl.bulk = false;
      tx.Send(ctrl);
    }
  }

  EXPECT_TRUE(WaitUntilDrained(tx))
      << "the outbox must drain through a link that EAGAINs";
  EXPECT_EQ(tx.dropped(), 0u);

  // pending_bytes()==0 means the endpoint handed everything to the kernel, not
  // that the peer has read it — let the reader catch up before tearing it down,
  // or the tail of the burst is lost to the test rather than to the code.
  std::vector<std::uint8_t> rx;
  for (int i = 0, stable = 0; i < 500 && stable < 20; ++i) {
    std::this_thread::sleep_for(1ms);
    const std::vector<std::uint8_t> chunk = reader.Take();
    rx.insert(rx.end(), chunk.begin(), chunk.end());
    stable = chunk.empty() ? stable + 1 : 0;
  }

  tx.Stop();
  reader.Stop();
  CloseFd(b);
  {
    const std::vector<std::uint8_t> tail = reader.Take();
    rx.insert(rx.end(), tail.begin(), tail.end());
  }

  const std::vector<Message> got = ExtractFrames(rx);
  int bulk_seen = 0;
  int ctrl_seen = 0;
  for (const Message& m : got) {
    if (m.stream_id == visio_schema::kCommand) {
      ++ctrl_seen;
      EXPECT_EQ(m.payload, "ctrl");
    } else {
      // Intact payload == the frames were never spliced into each other.
      EXPECT_EQ(m.payload.size(), 512u);
      EXPECT_EQ(m.seq, static_cast<std::uint32_t>(bulk_seen)) << "bulk order";
      ++bulk_seen;
    }
  }
  EXPECT_EQ(bulk_seen, kFrames);
  EXPECT_EQ(ctrl_seen, (kFrames + 9) / 10);
}

// A STALLED link (no bytes accepted while bytes are pending for the stall
// window) must shed bulk + decimatable frames at Send's door — counted by
// door_dropped() — while control streams AND non-decimatable data (one-shot
// events like ButtonEvent, ground-truth IMU bundles) still enqueue and are
// delivered when the reader comes back. The shed classes are what kept a dead
// serial gadget's I/O thread at ~5% CPU forever (see the Send gate comment);
// the queued classes are what recovery must not lose.
TEST(Backpressure, StalledLinkShedsDecimatableKeepsControlAndEvents) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);

  FramedFdEndpoint tx(a, WritePolicy::drop_oldest(/*depth=*/4096),
                      /*stall_ns=*/50'000'000);
  tx.Start({}, {});

  ASSERT_TRUE(FloodUntilLatched(tx, [&tx] { return tx.Stalled(); }))
      << "an unread peer must latch stalled()";
  // The interface-level view a producer's presence gate reads through the
  // bus roster (Endpoint::Stalled defaults false; framed endpoints forward
  // the latch).
  EXPECT_TRUE(static_cast<visio_schema::transport::Endpoint&>(tx).Stalled())
      << "Stalled() must reflect the latch through the Endpoint interface";

  // At the door while stalled: bulk and decimatable are refused before any
  // queue is touched; control and one-shot data enqueue. The decimatable
  // frame sits exactly on the control/data boundary id, so an off-by-one in
  // the gate can't hide.
  const std::uint64_t door_before = tx.door_dropped();
  const std::size_t pending_before = tx.pending_bytes();
  Message decim;
  decim.stream_id = visio_schema::kFirstDynamic;
  decim.payload = "DECIM-WHILE-STALLED";
  decim.decimatable = true;
  tx.Send(Frame(512));  // bulk while stalled
  tx.Send(decim);
  EXPECT_EQ(tx.door_dropped(), door_before + 2) << "bulk + decimatable refused";
  EXPECT_EQ(tx.pending_bytes(), pending_before) << "refused = never enqueued";

  Message ctrl;
  ctrl.stream_id = visio_schema::kHeartbeat;
  ctrl.payload = "CTRL-WHILE-STALLED";
  Message event;  // one-shot data (the ButtonEvent shape): must survive
  event.stream_id = visio_schema::kFirstDynamic + 1;
  event.payload = "EVENT-WHILE-STALLED";
  tx.Send(ctrl);
  tx.Send(event);

  // The reader comes back and drains; accepted writes lift the gate.
  DrainingReader reader(b);
  bool recovered = false;
  for (int i = 0; i < 2000 && !recovered; ++i) {
    recovered = !tx.Stalled();
    if (!recovered) std::this_thread::sleep_for(1ms);
  }
  EXPECT_TRUE(recovered) << "accepted writes must lift the stall gate";

  // With the gate lifted even the shed class flows again. It rides the same
  // FIFO control outbox as ctrl/event above, so once it arrives, anything
  // enqueued during the stall has arrived before it — the wait below cannot
  // truncate the assertions that follow.
  Message after;
  after.stream_id = visio_schema::kFirstDynamic;
  after.payload = "DECIM-AFTER-RECOVERY";
  after.decimatable = true;
  tx.Send(after);

  std::vector<Message> got;
  std::vector<std::uint8_t> carry;
  bool seen_after = false;
  for (int i = 0; i < 2000 && !seen_after; ++i) {
    const std::vector<std::uint8_t> chunk = reader.Take();
    carry.insert(carry.end(), chunk.begin(), chunk.end());
    for (Message& m : ExtractFrames(carry)) {  // consumes; keeps partial tail
      seen_after |= (m.payload == "DECIM-AFTER-RECOVERY");
      got.push_back(std::move(m));
    }
    if (!seen_after) std::this_thread::sleep_for(1ms);
  }

  bool seen_ctrl = false;
  bool seen_event = false;
  bool seen_stalled_decim = false;
  for (const Message& m : got) {
    seen_ctrl |= (m.payload == "CTRL-WHILE-STALLED");
    seen_event |= (m.payload == "EVENT-WHILE-STALLED");
    seen_stalled_decim |= (m.payload == "DECIM-WHILE-STALLED");
  }
  EXPECT_TRUE(seen_ctrl) << "control frames must survive the stalled window";
  EXPECT_TRUE(seen_event) << "one-shot data must survive the stalled window";
  EXPECT_FALSE(seen_stalled_decim)
      << "decimatable frames sent while stalled must be dropped at the door";
  EXPECT_TRUE(seen_after) << "shed classes must resume after recovery";

  tx.Stop();
  reader.Stop();
  CloseFd(b);
}

// A FIXED link that dies on a WRITE must report itself closed, exactly as a read
// EOF does. It cannot be reopened (Tick only reopens with a factory), so if this
// goes unreported the fd is gone, the endpoint is never polled again, and it
// strands attached to its owner forever while the peer is long gone.
//
// The ordering is the whole point. Loop() runs Pump() BEFORE ReadInbound(), and
// ReadInbound only runs when poll reported POLLIN/POLLHUP/POLLERR — so whenever
// the write fails on an iteration that reported POLLOUT *only*, the read path
// never gets to notice the peer and cannot act as a backstop. That is the real
// shape of the failure: a client aborting (RST) while the device still has video
// queued. Measured on an ego over NCM, a client RSTing with ~2 MB queued stranded
// its link within two attempts, after which the board (one client at a time)
// refused every later client until the process restarted.
//
// Reproduced deterministically here with a local shutdown(SHUT_WR): our writes
// fail EPIPE while the peer stays open and silent, so poll reports POLLOUT and
// nothing else — exactly the iteration shape that has no read-side rescue.
TEST(Backpressure, AFixedLinkDyingOnAWriteReportsClosed) {
    auto [a, b] = MakeFdPair();
    ASSERT_GE(a, 0);
    ASSERT_GE(b, 0);
    std::atomic<int> closed_calls{0};
    {
        FramedFdEndpoint ep(a);   // fixed fd: no factory, so no reopen is possible
        ep.Start([](Message, visio_schema::transport::Endpoint*) {},
                 [&closed_calls](visio_schema::transport::Endpoint*) {
                     closed_calls.fetch_add(1);
                 });
        // Writes now fail; the peer stays OPEN and silent so there is no POLLIN,
        // no POLLHUP and no POLLERR to fall back on.
        ASSERT_EQ(::shutdown(a, SHUT_WR), 0);
        for (int i = 0; i < 400 && closed_calls.load() == 0; ++i) {
            ep.Send(Frame(4096));
            std::this_thread::sleep_for(1ms);
        }
        EXPECT_EQ(closed_calls.load(), 1)
            << "a fixed link that died on a write never reported closed — the "
               "owner would keep it attached forever";
        ep.Stop();
    }
    // One-shot: the two report paths (read EOF, write failure) must never both
    // fire for one link.
    EXPECT_EQ(closed_calls.load(), 1);
    CloseFd(b);
}

// The read-EOF twin of the test above: dying on a READ must tear the link down
// the same way dying on a write does — reporting closed is not enough, the fd
// itself must close. When it didn't, a peer's half-close left the accepted
// socket in CLOSE_WAIT until the owner's deferred Stop(), and a phone redialing
// in a loop piled those up on the device.
//
// shutdown(b, SHUT_WR) is the probe: it delivers EOF to the endpoint while b
// stays open, so b sees a FIN back (read returns 0) only once the endpoint
// actually closes its fd — exactly the CLOSE_WAIT shape, made observable.
TEST(Backpressure, AFixedLinkDyingOnAReadEofClosesItsFd) {
    auto [a, b] = MakeFdPair();
    ASSERT_GE(a, 0);
    ASSERT_GE(b, 0);
    std::atomic<int> closed_calls{0};
    FramedFdEndpoint ep(a);  // fixed fd: no factory
    ep.Start([](Message, visio_schema::transport::Endpoint*) {},
             [&closed_calls](visio_schema::transport::Endpoint*) {
                 closed_calls.fetch_add(1);
             });
    ASSERT_EQ(::shutdown(b, SHUT_WR), 0);  // EOF to the endpoint; b stays open
    // The endpoint's next poll wake must close a: b then reads a FIN promptly.
    char c;
    long r = -1;
    for (int i = 0; i < 400; ++i) {
        r = ::recv(b, &c, 1, MSG_DONTWAIT);
        if (r == 0) break;  // FIN from a: the fd is really closed
        std::this_thread::sleep_for(1ms);
    }
    EXPECT_EQ(r, 0)
        << "a fixed link that saw read EOF reported closed but left its fd "
           "open — the socket sits in CLOSE_WAIT until the deferred reap";
    EXPECT_EQ(closed_calls.load(), 1);
    ep.Stop();
    EXPECT_EQ(closed_calls.load(), 1);  // one-shot across both report paths
    CloseFd(b);
}

namespace {

// Frame() with the sync-point mark: an H.265 keyframe. The congestion gate
// must always let these through — they are the only frames a degraded leg
// still owes its viewer.
Message Keyframe(std::size_t n) {
  Message m = Frame(n);
  m.keyframe = true;
  return m;
}

// Policy + windows shared by the congestion-tier tests: a byte cap tight
// enough that a flood evicts at enqueue, and a stall window far beyond any
// test's runtime so only the tier under test moves.
WritePolicy TightEvictionPolicy() {
  return WritePolicy::stale_eviction(16 * 1024, std::chrono::seconds(60));
}
constexpr std::int64_t kFarStallNs = 60'000'000'000;

}  // namespace

// An EVICTING leg (congested but not stalled) degrades to keyframes-only at
// the door: non-keyframe video is refused before any framing work, keyframes
// still enqueue. This is the tier that protects a shared-radio producer's
// capture path from a reader that limps along under the stall gate's radar.
TEST(Backpressure, EvictingLegDegradesVideoToKeyframesOnly) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);

  FramedFdEndpoint tx(a, TightEvictionPolicy(), kFarStallNs,
                      /*degrade_hold_ns=*/kFarStallNs);
  tx.Start({}, {});
  ASSERT_TRUE(FloodUntilLatched(tx, [&tx] { return tx.video_degraded(); }))
      << "an evicting video outbox must latch the keyframes-only gate";
  EXPECT_FALSE(tx.Stalled()) << "congestion tier must not require a stall";

  const std::uint64_t degraded_before = tx.degrade_dropped();
  const std::size_t pending_before = tx.pending_bytes();
  tx.Send(Frame(512));  // non-keyframe video while degraded
  EXPECT_EQ(tx.degrade_dropped(), degraded_before + 1);
  EXPECT_EQ(tx.pending_bytes(), pending_before) << "refused = never enqueued";

  tx.Send(Keyframe(512));  // the sync point must pass
  EXPECT_EQ(tx.degrade_dropped(), degraded_before + 1);
  EXPECT_GT(tx.pending_bytes(), pending_before) << "keyframes still enqueue";
  EXPECT_TRUE(tx.video_degraded()) << "hold not expired: gate stays latched";

  // The video gate touches nothing else: control, one-shot events and
  // decimatable data all enqueue while degraded (the stall tier pins the
  // same classes — StalledLinkShedsDecimatableKeepsControlAndEvents).
  const std::uint64_t door_before = tx.door_dropped();
  const std::size_t classes_pending_before = tx.pending_bytes();
  Message ctrl;
  ctrl.stream_id = visio_schema::kHeartbeat;
  ctrl.payload = "CTRL-WHILE-DEGRADED";
  Message event;
  event.stream_id = visio_schema::kFirstDynamic + 1;
  event.payload = "EVENT-WHILE-DEGRADED";
  Message decim;
  decim.stream_id = visio_schema::kFirstDynamic;
  decim.payload = "DECIM-WHILE-DEGRADED";
  decim.decimatable = true;
  tx.Send(ctrl);
  tx.Send(event);
  tx.Send(decim);
  EXPECT_EQ(tx.door_dropped(), door_before);
  EXPECT_EQ(tx.degrade_dropped(), degraded_before + 1);
  EXPECT_GT(tx.pending_bytes(), classes_pending_before);

  tx.Stop();
  CloseFd(b);
}

// Full rate resumes at the first KEYFRAME after an eviction-free hold — a
// sync point the viewer can decode from — never mid-GOP.
TEST(Backpressure, DegradedLegResumesFullRateAtKeyframeAfterHold) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);

  FramedFdEndpoint tx(a, TightEvictionPolicy(), kFarStallNs,
                      /*degrade_hold_ns=*/100'000'000);
  tx.Start({}, {});
  ASSERT_TRUE(FloodUntilLatched(tx, [&tx] { return tx.video_degraded(); }))
      << "an evicting video outbox must latch the keyframes-only gate";

  // The reader comes back and drains the backlog; with nothing left to evict
  // the hold runs out.
  DrainingReader reader(b);
  ASSERT_TRUE(WaitUntilDrained(tx, 2000));
  std::this_thread::sleep_for(200ms);  // > degrade_hold_ns, eviction-free

  // Expired hold alone must not resume: a P-frame is undecodable until the
  // next sync point, so the gate holds until a keyframe carries it out.
  const std::uint64_t degraded_before = tx.degrade_dropped();
  tx.Send(Frame(512));
  EXPECT_EQ(tx.degrade_dropped(), degraded_before + 1);
  EXPECT_TRUE(tx.video_degraded());

  tx.Send(Keyframe(512));  // first keyframe past the hold: resume here
  EXPECT_FALSE(tx.video_degraded());
  tx.Send(Frame(512));  // full rate again
  EXPECT_EQ(tx.degrade_dropped(), degraded_before + 1);

  reader.Stop();
  tx.Stop();
  CloseFd(b);
}

// A producer marks bulk frames `no_degrade` while some consumer may RECORD
// the stream (message.hpp — e.g. the phone recording-destination lease):
// those frames pass the keyframes-only gate untouched, so a congested leg
// can never thin a recording, while unmarked frames on the same latched leg
// still thin. Purely a per-frame field set by the producing process — never
// serialized, so no remote peer can set it, and nothing goes stale.
TEST(Backpressure, NoDegradeFramesBypassTheKeyframesOnlyGate) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);

  FramedFdEndpoint tx(a, TightEvictionPolicy(), kFarStallNs,
                      /*degrade_hold_ns=*/kFarStallNs);
  tx.Start({}, {});
  ASSERT_TRUE(FloodUntilLatched(tx, [&tx] { return tx.video_degraded(); }))
      << "an evicting video outbox must latch the keyframes-only gate";

  const std::uint64_t degraded_before = tx.degrade_dropped();
  const std::size_t pending_before = tx.pending_bytes();
  Message rec = Frame(512);  // non-keyframe bulk, marked as recorded
  rec.no_degrade = true;
  tx.Send(rec);
  EXPECT_EQ(tx.degrade_dropped(), degraded_before) << "marked frame must pass";
  EXPECT_GT(tx.pending_bytes(), pending_before);
  EXPECT_TRUE(tx.video_degraded()) << "the bypass must not lift the latch";

  tx.Send(Frame(512));  // unmarked non-keyframe on the same leg
  EXPECT_EQ(tx.degrade_dropped(), degraded_before + 1)
      << "unmarked frames still thin";

  tx.Stop();
  CloseFd(b);
}

// A reopenable endpoint (the serial gadget) starts each FRESH link
// undegraded: MarkLinkDead lifts the latch and resyncs the eviction cursor,
// so evictions charged to the dead link can never thin the first frames of
// the next one — a USB replug must not begin at one frame per GOP.
TEST(Backpressure, FreshLinkAfterReopenStartsUndegraded) {
  std::mutex mu;
  std::vector<int> peers;
  auto factory = [&mu, &peers]() {
    int sv[2];
    if (::socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) return -1;
    ShrinkBuffers(sv[0], sv[1]);
    std::lock_guard<std::mutex> lk(mu);
    peers.push_back(sv[1]);
    return sv[0];
  };
  FramedFdEndpoint tx(factory, TightEvictionPolicy(),
                      /*reopen_backoff_ns=*/1'000'000, kFarStallNs,
                      /*degrade_hold_ns=*/kFarStallNs);
  tx.Start({}, {});
  ASSERT_TRUE(FloodUntilLatched(tx, [&tx] { return tx.video_degraded(); }));

  // A last burst races extra evictions past the I/O thread's newest sample,
  // then the peer vanishes: MarkLinkDead must both lift the latch and adopt
  // the final eviction count as the fresh link's baseline.
  for (int i = 0; i < 8; ++i) tx.Send(Frame(2048));
  {
    std::lock_guard<std::mutex> lk(mu);
    ASSERT_EQ(peers.size(), 1u);
    CloseFd(peers[0]);
  }
  bool reopened = false;
  for (int i = 0; i < 3000 && !reopened; ++i) {
    std::this_thread::sleep_for(1ms);
    std::lock_guard<std::mutex> lk(mu);
    reopened = peers.size() >= 2;
  }
  ASSERT_TRUE(reopened) << "factory endpoint must self-heal";
  int fresh_peer = -1;
  {
    std::lock_guard<std::mutex> lk(mu);
    fresh_peer = peers.back();
  }
  DrainingReader reader(fresh_peer);

  // With a draining reader and no new evictions, the fresh link must read
  // undegraded as soon as the I/O thread has had a pass — a stale cursor
  // would re-latch here for the full (far-future) hold and time this out.
  bool undegraded = false;
  for (int i = 0; i < 2000 && !undegraded; ++i) {
    undegraded = !tx.video_degraded();
    if (!undegraded) std::this_thread::sleep_for(1ms);
  }
  EXPECT_TRUE(undegraded) << "a fresh link must not inherit the dead link's "
                             "congestion latch";
  const std::uint64_t before = tx.degrade_dropped();
  tx.Send(Frame(512));
  EXPECT_EQ(tx.degrade_dropped(), before);

  reader.Stop();
  tx.Stop();
}
// RequestVideoDegrade pre-arms the latch (endpoint.hpp): a connection made
// while the owner knows the leg's conditions are wedged starts keyframes-only
// from its very first frame — no burst window while the gate re-learns the
// congestion — and earns full rate back through the normal hold+keyframe path.
TEST(Backpressure, PreArmedEndpointStartsKeyframesOnlyAndRecovers) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);

  FramedFdEndpoint tx(a, TightEvictionPolicy(), kFarStallNs,
                      /*degrade_hold_ns=*/100'000'000);
  tx.RequestVideoDegrade();  // owner saw the previous link wedge
  tx.Start({}, {});
  DrainingReader reader(b);  // this reader is healthy: no evictions follow

  EXPECT_TRUE(tx.video_degraded());
  const std::size_t pending_before = tx.pending_bytes();
  tx.Send(Frame(512));  // the very first frame is already thinned
  EXPECT_EQ(tx.degrade_dropped(), 1u);
  EXPECT_EQ(tx.pending_bytes(), pending_before);

  std::this_thread::sleep_for(200ms);  // > hold, eviction-free on a healthy leg
  tx.Send(Keyframe(512));              // resume at the sync point
  EXPECT_FALSE(tx.video_degraded());
  tx.Send(Frame(512));
  EXPECT_EQ(tx.degrade_dropped(), 1u);

  reader.Stop();
  tx.Stop();
}

// The pre-armed latch is the SAME gate as the eviction-armed one: keyframes
// pass without lifting it, and no_degrade frames (a recording client
// reconnecting during churn — exactly who on_accept pre-arms) are never
// thinned. Pins the only thing standing between the carry-over feature and a
// thinned recording; a refactor giving pre-arm its own mechanism must fail
// here. degrade_dropped() is the "passed" signal — pending_bytes() drains
// asynchronously against a live reader and would race.
TEST(Backpressure, PreArmedGateStillPassesKeyframesAndNoDegradeFrames) {
  auto [a, b] = MakeFdPair();
  ASSERT_GE(a, 0);
  ASSERT_GE(b, 0);
  ShrinkBuffers(a, b);

  FramedFdEndpoint tx(a, TightEvictionPolicy(), kFarStallNs,
                      /*degrade_hold_ns=*/kFarStallNs);  // expiry can't race
  tx.RequestVideoDegrade();
  tx.Start({}, {});
  DrainingReader reader(b);

  tx.Send(Keyframe(512));
  EXPECT_EQ(tx.degrade_dropped(), 0u) << "keyframe passes while pre-armed";
  EXPECT_TRUE(tx.video_degraded()) << "...without lifting the latch";

  Message rec = Frame(512);
  rec.no_degrade = true;
  tx.Send(rec);  // the reconnecting recorder's frame
  EXPECT_EQ(tx.degrade_dropped(), 0u) << "never thinned, even pre-armed";

  tx.Send(Frame(512));  // unmarked viewer frame
  EXPECT_EQ(tx.degrade_dropped(), 1u) << "unmarked frames still thin";

  reader.Stop();
  tx.Stop();
}
