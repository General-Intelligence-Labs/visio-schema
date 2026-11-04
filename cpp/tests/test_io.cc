// io tests — SerialEndpoint over an fd pair round-trips a framed message, and a
// broken link surfaces via callbacks (endpoints are active objects that own their
// own I/O thread; they never self-reconnect on a fixed fd). Tests close the PEER
// fd (the endpoint owns + closes its own) to simulate a hangup.
#include <gtest/gtest.h>

#include <chrono>
#include <atomic>
#include <cerrno>
#include <sys/socket.h>
#include <unistd.h>
#include <thread>

#include "active_object_test_util.hpp"
#include "visio_schema/wire/time.hpp"
#include "visio_schema/transport/endpoint.hpp"
#include "visio_schema/transport/framed_fd.hpp"
#include "visio_schema/transport/link.hpp"
#include "visio_schema/transport/serial.hpp"

using visio_schema::transport::CloseFd;
using visio_schema::transport::MakeFdPair;
using visio_schema::transport::SerialEndpoint;
using visio_schema::transport::test::InboundCollector;
using visio_schema::wire::Message;

TEST(Io, SerialRoundTripsAFrame) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a), rx(b);
  InboundCollector rxc;
  rx.Start(rxc.fn(), rxc.on_closed());
  tx.Start(nullptr, nullptr);

  Message m;
  m.stream_id = 16;
  m.payload = "hello";
  tx.Send(m);

  ASSERT_GE(rxc.wait_for(1), 1u);
  auto got = rxc.messages();
  EXPECT_EQ(got[0].stream_id, 16u);
  EXPECT_EQ(got[0].payload, "hello");

  tx.Stop();
  rx.Stop();
}

// A fixed fd (no reconnect) reports peer EOF through the on_closed callback — it
// does not throw. The endpoint's I/O thread sees the hangup, fires the callback
// once, and exits.
TEST(Io, EofReportsClosed) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint rx(b);
  InboundCollector collector;
  rx.Start(collector.fn(), collector.on_closed());
  CloseFd(a);  // peer hangs up
  EXPECT_TRUE(collector.wait_closed());
  rx.Stop();
}

// A Send onto a broken link must never block or throw: the I/O thread drains the
// outbox best-effort, finds the fd dead, and sheds the frame. The caller's Send()
// is a thread-safe non-blocking enqueue regardless of link state.
TEST(Io, BrokenWriteSheddsWithoutThrowing) {
  auto [a, b] = MakeFdPair();
  SerialEndpoint tx(a);
  CloseFd(b);  // peer gone -> writes to `a` will EPIPE
  tx.Start(nullptr, nullptr);

  Message m;
  m.stream_id = 16;
  m.payload = "x";
  EXPECT_NO_THROW(tx.Send(m));

  std::this_thread::sleep_for(std::chrono::milliseconds(20));
  tx.Stop();
}

// Enqueue before the worker starts, proving the timestamp is taken at actual
// write rather than at producer enqueue. RX is stamped before dispatch.
TEST(Io, TimedSendStampsAtIoAndReceiveBeforeDispatch) {
  auto [a,b]=MakeFdPair();
  SerialEndpoint tx(a),rx(b);InboundCollector collector;
  Message msg;msg.stream_id=16;
  tx.SendTimed(msg,[](Message& m,std::int64_t stamp){
    visio_schema::SetTimestampNs(&m.timestamp,stamp);
  },{});
  const auto enqueue_done=visio_schema::MonotonicNs();
  std::this_thread::sleep_for(std::chrono::milliseconds(30));
  rx.Start(collector.fn(),collector.on_closed());tx.Start(nullptr,nullptr);
  ASSERT_GE(collector.wait_for(1),1u);const auto frame=collector.messages()[0];
  EXPECT_GT(visio_schema::TimestampNs(frame.timestamp),enqueue_done+20'000'000);
  EXPECT_GE(frame.receive_mono_ns,visio_schema::TimestampNs(frame.timestamp));
  tx.Stop();rx.Stop();
}

TEST(Io, TimedZeroByteRetriesRefreshTheStampAfterBackpressure) {
  auto [a,b]=MakeFdPair();
  int size=4096;ASSERT_EQ(setsockopt(a,SOL_SOCKET,SO_SNDBUF,&size,sizeof(size)),0);
  std::string filler(4096,'x');std::size_t filled=0;
  for(;;){const auto n=send(a,filler.data(),filler.size(),MSG_DONTWAIT);
    if(n<0){ASSERT_TRUE(errno==EAGAIN||errno==EWOULDBLOCK);break;}filled+=n;}
  std::atomic<unsigned> finalized{0};std::atomic<std::int64_t> sent{0};
  SerialEndpoint tx(a);Message m;m.stream_id=16;m.payload="timed";
  tx.SendTimed(m,[&](Message& frame,std::int64_t stamp){++finalized;visio_schema::SetTimestampNs(&frame.timestamp,stamp);},
      [&](std::int64_t stamp){sent.store(stamp);});tx.Start(nullptr,nullptr);
  for(int i=0;i<1000&&!finalized.load();++i)std::this_thread::sleep_for(std::chrono::milliseconds(1));
  ASSERT_GT(finalized.load(),0u);EXPECT_EQ(sent.load(),0);
  const auto release=visio_schema::MonotonicNs();
  while(filled){char bytes[4096];const auto n=read(b,bytes,std::min(filled,sizeof(bytes)));
    if(n<0&&(errno==EAGAIN||errno==EWOULDBLOCK)){std::this_thread::sleep_for(std::chrono::milliseconds(1));continue;}
    ASSERT_GT(n,0);filled-=n;}
  SerialEndpoint rx(b);InboundCollector collector;rx.Start(collector.fn(),collector.on_closed());
  ASSERT_GE(collector.wait_for(1),1u);const auto got=collector.messages()[0];
  EXPECT_GE(visio_schema::TimestampNs(got.timestamp),release);
  EXPECT_EQ(visio_schema::TimestampNs(got.timestamp),sent.load());EXPECT_GE(finalized.load(),2u);
  tx.Stop();rx.Stop();
}

TEST(Io, TimedPartialFrameKeepsItsFirstStampAndCannotInterleave) {
  auto [a,b]=MakeFdPair();int size=4096;ASSERT_EQ(setsockopt(a,SOL_SOCKET,SO_SNDBUF,&size,sizeof(size)),0);
  std::atomic<unsigned> finalized{0};std::atomic<std::int64_t> sent{0};
  SerialEndpoint tx(a);Message m;m.stream_id=16;m.payload=std::string(200000,'q');
  tx.SendTimed(m,[&](Message& frame,std::int64_t stamp){++finalized;visio_schema::SetTimestampNs(&frame.timestamp,stamp);},
      [&](std::int64_t stamp){sent.store(stamp);});tx.Start(nullptr,nullptr);
  for(int i=0;i<1000&&!sent.load();++i)std::this_thread::sleep_for(std::chrono::milliseconds(1));
  ASSERT_GT(sent.load(),0);Message other;other.stream_id=1;other.payload="next";tx.Send(other);
  SerialEndpoint rx(b);InboundCollector collector;rx.Start(collector.fn(),collector.on_closed());
  ASSERT_GE(collector.wait_for(2),2u);auto got=collector.messages();
  EXPECT_EQ(got[0].payload,m.payload);EXPECT_EQ(got[1].payload,other.payload);
  EXPECT_EQ(visio_schema::TimestampNs(got[0].timestamp),sent.load());EXPECT_EQ(finalized.load(),1u);
  tx.Stop();rx.Stop();
}

TEST(Io, TimedPartialWritesCountAsProgressRatherThanAStalledLink) {
  auto [a,b]=MakeFdPair();
  int size=4096;ASSERT_EQ(setsockopt(a,SOL_SOCKET,SO_SNDBUF,&size,sizeof(size)),0);
  visio_schema::transport::FramedFdEndpoint tx(
      a,visio_schema::transport::WritePolicy::drop_oldest(16),200'000'000);
  Message m;m.stream_id=16;m.payload=std::string(200000,'p');
  tx.SendTimed(m,[](Message&,std::int64_t){},{});tx.Start(nullptr,nullptr);
  std::size_t read_bytes=0;bool stalled=false;
  const auto start=std::chrono::steady_clock::now();
  while(read_bytes<200000 && std::chrono::steady_clock::now()-start<std::chrono::seconds(5)){
    std::uint8_t bytes[256];const auto n=visio_schema::transport::ReadSome(b,bytes,sizeof(bytes));
    ASSERT_GE(n,0);read_bytes+=static_cast<std::size_t>(n);
    stalled=stalled||tx.Stalled();
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  }
  EXPECT_GE(read_bytes,200000u);EXPECT_FALSE(stalled);
  tx.Stop();CloseFd(b);
}
