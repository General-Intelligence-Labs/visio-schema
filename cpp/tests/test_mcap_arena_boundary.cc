// Actual endpoint storage/reclaim boundaries; no writer or arena is doubled.
#include <gtest/gtest.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "mcap_arena_boundary.hpp"
#include "mcap_writer_test_util.hpp"
#include "visio_schema/log.hpp"
#include "visio_schema/mcap/writer_endpoint.hpp"
#include "visio_schema/wire/control.hpp"
#include "visio_schema/wire/time.hpp"

#define MCAP_COMPRESSION_NO_LZ4
#define MCAP_COMPRESSION_NO_ZSTD
#define MCAP_IMPLEMENTATION
#define mcap arena_boundary_mcap
#include <mcap/reader.hpp>
#undef mcap
#undef MCAP_IMPLEMENTATION

namespace {
using namespace mcap_arena_test;
using namespace visio_schema;
using namespace visio_schema::mcap;
constexpr std::size_t kFrameBytes = 200u << 10;

struct Frame {
  unsigned sequence;
  std::int64_t timestamp;
  std::string payload;
  bool keyframe = false;
};

std::vector<Frame> Frames(unsigned initial, bool short_fourth,
                          unsigned following = 6) {
  std::vector<Frame> frames;
  for (unsigned i = 0; i < initial + following; ++i) {
    Frame frame{101 + i, 9'000'000'000LL + i * 33'333'333LL,
                "frame:" + std::to_string(101 + i) + ":"};
    frame.payload.resize(short_fourth && i == 3 ? 32u << 10 : kFrameBytes,
                         static_cast<char>('a' + i));
    frame.payload.back() = static_cast<char>('A' + i);
    frames.push_back(std::move(frame));
  }
  return frames;
}

bool Send(McapWriterEndpoint& endpoint, const Frame& frame) {
  auto bytes = std::make_shared<const std::string>(frame.payload);
  wire::Message message;
  message.stream_id = kFirstDynamic;
  message.seq = frame.sequence;
  message.keyframe = frame.sequence == 101 || frame.keyframe;
  SetTimestampNs(&message.timestamp, frame.timestamp);
  message.payload = wire::Payload(bytes);
  endpoint.Send(message);
  message.payload = wire::Payload{};
  return bytes.use_count() == 1;  // real arena copied, no heap fallback
}

class Running {
 public:
  Running(McapWriterEndpoint& endpoint, Gate& append, Gate* reclaim = nullptr)
      : endpoint_(endpoint), append_(append), reclaim_(reclaim) {}
  ~Running() {
    append_.Release();
    if (reclaim_) reclaim_->Release();
    Join();
    endpoint_.Stop();
  }
  template <typename Action>
  void Produce(Action action) {
    producers_.emplace_back(std::move(action));
  }
  void Protect(Gate& reclaim) { reclaim_ = &reclaim; }
  void Join() {
    for (auto& producer : producers_)
      if (producer.joinable()) producer.join();
  }

 private:
  McapWriterEndpoint& endpoint_;
  Gate& append_;
  Gate* reclaim_;
  std::vector<std::thread> producers_;
};

class McapArenaBoundary : public testing::Test {
 protected:
  void SetUp() override {
    directory_ = mcap_test::TempPath("mcap_arena_boundary_XXXXXX");
    ASSERT_NE(::mkdtemp(directory_.data()), nullptr);
    created_ = true;
    path_ = directory_ + "/part.mcap";
    channel_.schema_name = "foxglove.CompressedVideo";
  }
  void TearDown() override {
    if (created_) std::filesystem::remove_all(directory_);
  }
  void ExpectRecording(const std::vector<Frame>& frames) {
    const auto bytes = mcap_test::SlurpFile(path_);
    ASSERT_TRUE(mcap_test::EndsWithMcapMagic(bytes));
    arena_boundary_mcap::BufferReader input;
    input.reset(reinterpret_cast<const std::byte*>(bytes.data()), bytes.size(),
                bytes.size());
    arena_boundary_mcap::McapReader reader;
    ASSERT_TRUE(reader.open(input).ok());
    const auto problem = [](const arena_boundary_mcap::Status& status) {
      ADD_FAILURE() << status.message;
    };
    std::size_t count = 0;
    for (const auto& row : reader.readMessages(problem)) {
      ASSERT_LT(count, frames.size()) << "unexpected/duplicate record";
      const auto& expected = frames[count++];
      EXPECT_EQ(row.channel->topic, channel_.topic);
      EXPECT_EQ(row.message.sequence, expected.sequence);
      EXPECT_EQ(row.message.logTime, expected.timestamp);
      EXPECT_EQ(row.message.publishTime, expected.timestamp);
      const std::string payload(reinterpret_cast<const char*>(row.message.data),
                                row.message.dataSize);
      EXPECT_TRUE(payload == expected.payload)
          << "payload identity/content mismatch, frame " << expected.sequence;
    }
    EXPECT_EQ(count, frames.size()) << "missing/interior/terminal record";
  }
  static auto Policy() {
    // Same 16MiB byte-bound/cadence-independent policy as the firmware
    // recorder.
    return transport::WritePolicy::stale_eviction(16u << 20,
                                                  std::chrono::milliseconds(0));
  }
  static void ExpectStats(McapWriterEndpoint& endpoint, std::size_t count,
                          std::size_t dropped = 0,
                          std::size_t fallbacks = 0) {
    RecordProperty("arena_object_bytes", std::to_string(sizeof(PayloadArena)));
    RecordProperty("record_object_bytes",
                   std::to_string(sizeof(PayloadArena::Record)));
    RecordProperty("endpoint_object_bytes",
                   std::to_string(sizeof(McapWriterEndpoint)));
    const auto stats = endpoint.stats();
    EXPECT_FALSE(endpoint.write_failed());
    EXPECT_FALSE(endpoint.storage_fault());
    EXPECT_EQ(stats.writes, count);
    EXPECT_EQ(stats.video_frames, count);
    EXPECT_EQ(stats.dropped, dropped);
    EXPECT_EQ(stats.arena_fallbacks, fallbacks);
    EXPECT_EQ(stats.arena_capacity_bytes, 32u << 20);
    EXPECT_EQ(endpoint.pending_bytes(), 0u);
  }

  static void ExpectWait(const WaitProbe::Observation& before,
                         const WaitProbe::Observation& after,
                         unsigned id = 0) {
    const auto prefix = "capacity_wait_" + std::to_string(id) + "_";
    RecordProperty(prefix + "entry_count", std::to_string(before.entries));
    RecordProperty(prefix + "returns_before_action",
                   std::to_string(before.returns));
    RecordProperty(prefix + "final_entries", std::to_string(after.entries));
    RecordProperty(prefix + "final_returns", std::to_string(after.returns));
    const auto condition = reinterpret_cast<std::uintptr_t>(before.condition);
    const auto mutex = reinterpret_cast<std::uintptr_t>(before.mutex);
    RecordProperty(prefix + "condition", std::to_string(condition));
    RecordProperty(prefix + "mutex", std::to_string(mutex));
    std::ostringstream producer;
    producer << before.producer;
    RecordProperty(prefix + "producer_thread", producer.str());
    RecordProperty(
        prefix + "return_code",
        after.result ? std::to_string(*after.result) : "unobserved");
    EXPECT_GT(before.entries, 0u);
    EXPECT_NE(before.condition, nullptr);
    EXPECT_NE(before.mutex, nullptr);
    EXPECT_NE(before.producer, std::thread::id{});
    EXPECT_FALSE(before.finished);
    EXPECT_TRUE(before.same_identity);
    EXPECT_TRUE(after.same_identity);
    EXPECT_TRUE(after.finished);
    EXPECT_EQ(after.producer, before.producer);
    EXPECT_EQ(after.condition, before.condition);
    EXPECT_EQ(after.mutex, before.mutex);
    EXPECT_GE(after.entries, before.entries);
    EXPECT_EQ(after.entries, after.returns);
    EXPECT_TRUE(after.result.has_value());
    if (after.result) EXPECT_EQ(*after.result, 0);
  }

  // All initial records occupy one chunk. Completed writes have already
  // popped theirs before the current Write enters the real Append gate.
  std::size_t HoldFullArena(McapWriterEndpoint& endpoint,
                            const std::shared_ptr<Storage>& storage,
                            const std::vector<Frame>& frames,
                            std::size_t initial, Running& running,
                            std::unique_ptr<ReclaimProbe>& probe) {
    for (std::size_t i = 0; i < initial; ++i)
      if (!Send(endpoint, frames[i])) return 0;
    storage->append.Arm();
    endpoint.Start(nullptr, nullptr);
    if (!storage->append.WaitEntered()) return 0;
    const auto written = endpoint.stats().writes;
    if (written < 2 || written >= initial) return 0;
    const auto queued = 128 - (initial - written);
    if (queued * kFrameBytes > (16u << 20)) return 0;
    for (std::size_t i = initial; i < initial + queued; ++i)
      if (!Send(endpoint, frames[i])) return 0;
    EXPECT_EQ(endpoint.pending_frames(), queued);
    EXPECT_EQ(endpoint.stats().arena_fallbacks, 0u);
    RecordProperty("live_chunks_before_release", "128");
    RecordProperty("writes_at_append_gate", std::to_string(written));
    RecordProperty("queued_at_append_gate", std::to_string(queued));
    probe = std::make_unique<ReclaimProbe>([&endpoint] {
      return endpoint.stats().writes;
    });
    running.Protect(probe->reclaim);
    storage->append.Release();
    if (!probe->reclaim.WaitEntered()) return 0;
    EXPECT_EQ(probe->writes_at_entry(), written + 1);
    return initial + queued;
  }

  std::string directory_, path_;
  bool created_ = false;
  Channel channel_ = mcap_test::MakeChannel(kFirstDynamic, "/ego/camera/0");
  StreamResolver resolve_ = [this](std::uint32_t id) -> const Channel* {
    return id == channel_.id ? &channel_ : nullptr;
  };
};

TEST_F(McapArenaBoundary, SendEvictionFinishesHandbackWithoutWriterService) {
  auto frames = Frames(100, false, 0);
  for (auto& frame : frames) frame.keyframe = true;
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, false,
                              0, 0, {}, {}, storage);
  ReclaimProbe probe([] { return 0; });
  probe.reclaim.Release();
  Running running(endpoint, storage->append, &probe.reclaim);
  for (const auto& frame : frames) EXPECT_TRUE(Send(endpoint, frame));
  EXPECT_GT(probe.calls(), 0u) << "Send left handback for future service";
  EXPECT_EQ(endpoint.stats().arena_fallbacks, 0u);
  endpoint.Start(nullptr, nullptr);
  endpoint.Stop();
  constexpr std::size_t kept = (16u << 20) / kFrameBytes;
  const std::vector<Frame> expected(frames.end() - kept, frames.end());
  ExpectStats(endpoint, kept, frames.size() - kept);
  ExpectRecording(expected);
}

TEST_F(McapArenaBoundary, FullArenaWaitsThenCopiesWithoutNewFallback) {
  const auto frames = Frames(164, false, 0);
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, false,
                              0, 0, {}, {}, storage);
  std::unique_ptr<ReclaimProbe> probe;
  WaitProbe waits;
  Gate finished;
  bool copied = false;
  Running running(endpoint, storage->append);
  const auto count = HoldFullArena(endpoint, storage, frames, 64,
                                    running, probe);
  ASSERT_GT(count, 0u) << "full actual arena boundary was not established";
  running.Produce([&] {
    {
      WaitProbe::Scope scope(waits, 0);
      copied = Send(endpoint, frames[count]);
    }
    finished.Sent();
  });
  const bool entered = waits.WaitEntered(1);
  const auto before = waits.Snapshot(0);
  const bool early = finished.WaitSent();
  probe->reclaim.Release();
  running.Join();
  EXPECT_TRUE(entered) << "producer never reached actual capacity wait";
  ExpectWait(before, waits.Snapshot(0));
  EXPECT_FALSE(early) << "full arena admitted before its handback";
  EXPECT_TRUE(copied);
  endpoint.Stop();
  ExpectStats(endpoint, count + 1);
  ExpectRecording(std::vector<Frame>(frames.begin(), frames.begin() + count + 1));
}

TEST_F(McapArenaBoundary, StopWakesEveryFullArenaProducerBeforeHandback) {
  const auto frames = Frames(164, false, 0);
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, false,
                              0, 0, {}, {}, storage);
  std::unique_ptr<ReclaimProbe> probe;
  WaitProbe waits;
  Gate finished[2];
  Running running(endpoint, storage->append);
  const auto count = HoldFullArena(endpoint, storage, frames, 64,
                                    running, probe);
  ASSERT_GT(count, 0u);
  for (unsigned i = 0; i < 2; ++i) {
    running.Produce([&, i] {
      {
        WaitProbe::Scope scope(waits, i);
        Send(endpoint, frames[count + i]);
      }
      finished[i].Sent();
    });
  }
  bool entered = true;
  for (unsigned i = 0; i < 2; ++i)
    entered = waits.WaitEntered(i + 1) && entered;
  const WaitProbe::Observation before[] = {
      waits.Snapshot(0), waits.Snapshot(1)};
  if (!entered) {
    probe->reclaim.Release();
    running.Join();
  }
  ASSERT_TRUE(entered) << "two producers never reached actual capacity wait";
  bool early[2], woke[2];
  for (unsigned i = 0; i < 2; ++i)
    early[i] = finished[i].WaitSent();
  const bool stopped = endpoint.StopFor(std::chrono::milliseconds(0));
  for (unsigned i = 0; i < 2; ++i) woke[i] = finished[i].WaitSent();
  const WaitProbe::Observation after[] = {
      waits.Snapshot(0), waits.Snapshot(1)};
  const bool premature = endpoint.finalized();
  probe->reclaim.Release();
  running.Join();
  EXPECT_FALSE(stopped);
  EXPECT_FALSE(premature);
  EXPECT_NE(before[0].producer, before[1].producer);
  EXPECT_EQ(before[0].condition, before[1].condition);
  EXPECT_EQ(before[0].mutex, before[1].mutex);
  for (unsigned i = 0; i < 2; ++i) {
    ExpectWait(before[i], after[i], i);
    EXPECT_FALSE(early[i]);
    EXPECT_TRUE(woke[i]) << "capacity waiter did not observe stop";
  }
  endpoint.Stop();
  ExpectStats(endpoint, count);
  ExpectRecording(std::vector<Frame>(frames.begin(), frames.begin() + count));
}

TEST_F(McapArenaBoundary, EvictionWakesCapacityWaiterDuringWriterHandback) {
  auto frames = Frames(164, false, 0);
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, false,
                              0, 0, {}, {}, storage);
  std::unique_ptr<ReclaimProbe> probe;
  WaitProbe waits;
  Gate finished;
  bool copied = false;
  Running running(endpoint, storage->append);
  const auto count = HoldFullArena(endpoint, storage, frames, 50,
                                    running, probe);
  ASSERT_GT(count, 0u);
  ASSERT_EQ(count - 50, 81u) << "oversized eviction precondition changed";
  running.Produce([&] {
    {
      WaitProbe::Scope scope(waits, 0);
      copied = Send(endpoint, frames[count + 1]);
    }
    finished.Sent();
  });
  const bool entered = waits.WaitEntered(1);
  const auto before = waits.Snapshot(0);
  if (!entered) {
    probe->reclaim.Release();
    running.Join();
  }
  ASSERT_TRUE(entered) << "producer never reached actual capacity wait";
  const bool early = finished.WaitSent();
  frames[count].payload.resize(300u << 10, 'X');
  const bool oversized_copied = Send(endpoint, frames[count]);
  const bool woke = finished.WaitSent();
  const auto after = waits.Snapshot(0);
  probe->reclaim.Release();
  running.Join();
  ExpectWait(before, after);
  EXPECT_FALSE(early);
  EXPECT_FALSE(oversized_copied);
  EXPECT_TRUE(woke) << "eviction made capacity without notifying its waiter";
  EXPECT_TRUE(copied);
  endpoint.Stop();
  std::vector<Frame> expected(frames.begin(), frames.begin() + 50);
  expected.insert(expected.end(), frames.begin() + 52, frames.begin() + count + 2);
  ExpectStats(endpoint, count, 2, 1);
  ExpectRecording(expected);
}

TEST_F(McapArenaBoundary, SendCompletesWhileActualStorageWriteIsHeld) {
  const auto frames = Frames(6, false);  // exceeds the actual 768KiB MCAP chunk
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, true, 0, 0,
                              {}, {}, storage);
  std::atomic<bool> copied{true};
  Running running(endpoint, storage->append);
  for (unsigned i = 0; i < 6; ++i) ASSERT_TRUE(Send(endpoint, frames[i]));
  storage->append.Arm();  // header creation already completed off the gate
  endpoint.Start(nullptr, nullptr);
  ASSERT_TRUE(storage->append.WaitEntered()) << "actual Append not reached";
  running.Produce([&] {
    for (std::size_t i = 6; i < frames.size(); ++i)
      if (!Send(endpoint, frames[i])) copied = false;
    storage->append.Sent();
  });
  const bool guard_completed = storage->append.WaitSent();
  storage->append.Release();
  running.Join();
  RecordProperty("producer_before_release",
                 storage->append.SentBeforeRelease() ? "true" : "false");
  EXPECT_TRUE(storage->append.SentBeforeRelease())
      << "Send depended on storage completion; hang guard=" << guard_completed;
  EXPECT_TRUE(copied);
  endpoint.Stop();
  ExpectStats(endpoint, frames.size());
  ExpectRecording(frames);
}

TEST_F(McapArenaBoundary, SendCompletesBeforeThirdChunkMadviseIsReleased) {
  // Two records share chunk2; handing it back after the first is also wrong.
  const auto frames = Frames(4, true);
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, true, 0, 0,
                              {}, {}, storage);
  ReclaimProbe probe([&] { return endpoint.stats().writes; });
  std::atomic<bool> copied{true};
  Running running(endpoint, storage->append, &probe.reclaim);
  for (unsigned i = 0; i < 4; ++i) ASSERT_TRUE(Send(endpoint, frames[i]));
  endpoint.Start(nullptr, nullptr);
  ASSERT_TRUE(probe.reclaim.WaitEntered())
      << "actual chunk hand-back not reached";
  EXPECT_EQ(probe.writes_at_entry(), 4u) << "reclaimed while a record was live";
  const auto& snapshot = probe.entry_bytes();
  ASSERT_EQ(snapshot.size(), 256u << 10);
  EXPECT_TRUE(std::string(snapshot.data(), kFrameBytes) == frames[2].payload);
  EXPECT_TRUE(std::string(snapshot.data() + kFrameBytes, 32u << 10) ==
              frames[3].payload);
  running.Produce([&] {
    // More than two chunks: an unsafe off-lock Free would reuse chunk2 here.
    for (std::size_t i = 4; i < frames.size(); ++i)
      if (!Send(endpoint, frames[i])) copied = false;
    probe.reclaim.Sent();
  });
  const bool guard_completed = probe.reclaim.WaitSent();
  probe.reclaim.Release();
  running.Join();
  RecordProperty("producer_before_release",
                 probe.reclaim.SentBeforeRelease() ? "true" : "false");
  EXPECT_TRUE(probe.reclaim.SentBeforeRelease())
      << "Send depended on madvise completion; hang guard=" << guard_completed;
  EXPECT_TRUE(copied);
  endpoint.Stop();
  RecordProperty("writes_at_reclaim_entry",
                 std::to_string(probe.writes_at_entry()));
  RecordProperty("reclaim_range_unchanged",
                 !probe.checked_after_send() ? "UNOBSERVED"
                 : probe.range_unchanged()   ? "true"
                                             : "false");
  EXPECT_GE(probe.calls(), 1u);
  EXPECT_EQ(probe.checked_after_send(), probe.reclaim.SentBeforeRelease());
  if (probe.checked_after_send())
    EXPECT_TRUE(probe.range_unchanged()) << "pending reclaim range was reused";
  EXPECT_EQ(probe.syscall_result(), 0);
  ExpectStats(endpoint, frames.size());
  ExpectRecording(frames);
}
TEST_F(McapArenaBoundary, StopBudgetIsAvailableWhileAReclaimIsHeld) {
  auto frames = Frames(4, true);
  frames.resize(4);
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, true, 0, 0,
                              {}, {}, storage);
  ReclaimProbe probe([&] { return endpoint.stats().writes; });
  Gate budget;
  std::atomic<bool> stopped{true};
  Running running(endpoint, storage->append, &probe.reclaim);
  for (const auto& frame : frames) ASSERT_TRUE(Send(endpoint, frame));
  endpoint.Start(nullptr, nullptr);
  ASSERT_TRUE(probe.reclaim.WaitEntered());
  running.Produce([&] {
    stopped = endpoint.StopFor(std::chrono::milliseconds(2));
    budget.Sent();
  });
  const bool guard_completed = budget.WaitSent();
  budget.Release();
  probe.reclaim.Release();
  running.Join();
  EXPECT_TRUE(budget.SentBeforeRelease()) << "hang guard=" << guard_completed;
  EXPECT_FALSE(stopped) << "pending syscall still owned the mapping";
  endpoint.Stop();
  EXPECT_EQ(probe.syscall_result(), 0);
  ExpectStats(endpoint, frames.size());
  ExpectRecording(frames);
}

TEST_F(McapArenaBoundary, QuietDrainReturnsChunkPagesBeforeStop) {
  auto frames = Frames(4, true);
  frames.resize(4);
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, true, 0, 0,
                              {}, {}, storage);
  ReclaimProbe probe([&] { return endpoint.stats().writes; });
  Running running(endpoint, storage->append, &probe.reclaim);
  for (const auto& frame : frames) ASSERT_TRUE(Send(endpoint, frame));
  endpoint.Start(nullptr, nullptr);
  ASSERT_TRUE(probe.reclaim.WaitEntered());
  const auto page = ::sysconf(_SC_PAGESIZE);
  ASSERT_GT(page, 0);
  std::vector<unsigned char> residency(((256u << 10) + page - 1) / page);
  ASSERT_EQ(::mincore(const_cast<char*>(probe.address()), 256u << 10,
                      residency.data()),
            0);
  EXPECT_TRUE(std::any_of(residency.begin(), residency.end(),
                          [](auto value) { return value & 1; }));
  RecordProperty(
      "reclaim_region_pages_before",
      std::to_string(std::count_if(residency.begin(), residency.end(),
                                   [](auto v) { return v & 1; })));
  probe.reclaim.Release();
  ASSERT_TRUE(probe.returned.WaitSent());
  EXPECT_EQ(probe.syscall_result(), 0);
  ASSERT_EQ(::mincore(const_cast<char*>(probe.address()), 256u << 10,
                      residency.data()),
            0);
  EXPECT_TRUE(std::none_of(residency.begin(), residency.end(),
                           [](auto value) { return value & 1; }));
  RecordProperty(
      "reclaim_region_pages_after",
      std::to_string(std::count_if(residency.begin(), residency.end(),
                                   [](auto v) { return v & 1; })));
  EXPECT_FALSE(endpoint.finalized());
  endpoint.Stop();
  ExpectStats(endpoint, frames.size());
  ExpectRecording(frames);
}

TEST_F(McapArenaBoundary, EvictionCanReuseUnclaimedChunksWhileReclaimIsHeld) {
  const auto frames = Frames(4, true, 100);
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, true, 0, 0,
                              {}, {}, storage);
  ReclaimProbe probe([&] { return endpoint.stats().writes; });
  std::atomic<bool> copied{true};
  Running running(endpoint, storage->append, &probe.reclaim);
  for (unsigned i = 0; i < 4; ++i) ASSERT_TRUE(Send(endpoint, frames[i]));
  endpoint.Start(nullptr, nullptr);
  ASSERT_TRUE(probe.reclaim.WaitEntered());
  running.Produce([&] {
    for (std::size_t i = 4; i < frames.size(); ++i)
      if (!Send(endpoint, frames[i])) copied = false;
    probe.reclaim.Sent();
  });
  const bool guard_completed = probe.reclaim.WaitSent();
  probe.reclaim.Release();
  running.Join();
  EXPECT_TRUE(probe.reclaim.SentBeforeRelease())
      << "hang guard=" << guard_completed;
  EXPECT_TRUE(copied);
  endpoint.Stop();
  EXPECT_TRUE(probe.checked_after_send());
  EXPECT_TRUE(probe.range_unchanged());
  // The writer was held: exactly 81 full frames fit, 19 oldest frames evict.
  constexpr std::size_t kept = (16u << 20) / kFrameBytes;
  std::vector<Frame> expected(frames.begin(), frames.begin() + 4);
  expected.insert(expected.end(), frames.end() - kept, frames.end());
  ExpectStats(endpoint, expected.size(), 100 - kept);
  EXPECT_EQ(endpoint.stats().max_pending_bytes, kept * kFrameBytes);
  ExpectRecording(expected);
}

std::atomic<unsigned> reclaim_warnings{0};
void CountReclaimWarning(const visio_schema::log::Record& record) {
  if (record.text.find("PayloadArena: madvise(DONTNEED) failed") !=
      std::string_view::npos)
    ++reclaim_warnings;
}
class ReclaimWarnings {
 public:
  ReclaimWarnings() {
    reclaim_warnings = 0;
    visio_schema::log::SetSink(CountReclaimWarning);
  }
  ~ReclaimWarnings() { visio_schema::log::SetSink(nullptr); }
};

TEST_F(McapArenaBoundary,
       ReclaimFailureRetainsPagesUntilReuseWithoutRetrySpam) {
  ReclaimWarnings warnings;
  ReclaimProbe probe([] { return 0; }, EIO);
  probe.reclaim.Release();
  constexpr std::size_t chunk = 256u << 10;
  // A fresh arena resets warning ownership; within each, retries require reuse.
  for (unsigned restart = 0; restart < 2; ++restart) {
    PayloadArena arena(3 * chunk, chunk, 2);
    auto a = arena.Put(std::string(chunk, 'a'));
    auto b = arena.Put(std::string(chunk, 'b'));
    auto c = arena.Put(std::string(chunk, 'c'));
    ASSERT_TRUE(a && b && c);
    for (unsigned use = 0; use < 2; ++use) {
      auto reclaim = c->Release();
      c.reset();
      ASSERT_TRUE(reclaim);
      EXPECT_EQ(reclaim->Run(), EIO);
      EXPECT_EQ(probe.calls(), restart * 3 + use + 1);
      EXPECT_FALSE(arena.Put("still quarantined"));
      arena.CompleteReclaim(std::move(*reclaim), EIO);
      EXPECT_EQ(arena.resident_bytes(), 3 * chunk);
      EXPECT_TRUE(arena.CanPut(chunk));  // no retry without new use
      EXPECT_EQ(reclaim_warnings, restart + 1);
      c = arena.Put(std::string(chunk, 'd'));
      ASSERT_TRUE(c);
      EXPECT_EQ(c->bytes(), std::string(chunk, 'd'));
      EXPECT_EQ(a->bytes(), std::string(chunk, 'a'));
      EXPECT_EQ(b->bytes(), std::string(chunk, 'b'));
    }
    // The final new use also owns an immediate destructor handback.
    c.reset();
    EXPECT_EQ(probe.calls(), (restart + 1) * 3);
  }
  EXPECT_EQ(probe.calls(), 6u);
  EXPECT_EQ(reclaim_warnings, 2u);
}

TEST_F(McapArenaBoundary, InlineThreadFailureKeepsStopBudgetAvailable) {
  const auto frames = Frames(4, true, 0);
  const pid_t child = ::fork();
  ASSERT_GE(child, 0);
  if (child == 0) {
    // Only this disposable child loses permission to create more threads.
    // Existing helper threads remain available to release/join every gate.
    ::alarm(12);
    auto storage = std::make_shared<Storage>();
    McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, false,
                                0, 0, {}, {}, storage);
    ReclaimProbe probe([] { return 0; });
    for (const auto& frame : frames)
      if (!Send(endpoint, frame)) ::_exit(31);
    if (::geteuid() == 0 && ::setuid(65534) != 0) ::_exit(32);
    Gate begin, budget;
    std::atomic<bool> budget_before_release{false};
    std::thread finalizer([&] {
      begin.WaitSent();
      endpoint.Stop();
    });
    std::thread releaser([&] {
      const bool entered = probe.reclaim.WaitEntered();
      budget_before_release = entered && budget.WaitSent();
      budget.Release();
      probe.reclaim.Release();
    });
    const rlimit no_threads{0, 0};
    if (::setrlimit(RLIMIT_NPROC, &no_threads) != 0) ::_exit(33);
    begin.Sent();
    const bool entered = probe.reclaim.WaitEntered();
    const bool stopped = endpoint.StopFor(std::chrono::milliseconds(0));
    const bool premature = endpoint.finalized();
    budget.Sent();
    finalizer.join();
    releaser.join();
    const bool valid = entered && budget_before_release && !stopped &&
        !premature && endpoint.finalized() && endpoint.write_failed() &&
        endpoint.pending_bytes() == 0 && endpoint.dropped_frames() == frames.size() &&
        probe.calls() == 1;
    ::alarm(0);
    if (!valid) {
      std::fprintf(stderr, "inline finalization: entered=%d budget=%d "
                   "stopped=%d premature=%d calls=%u\n", entered,
                   budget_before_release.load(), stopped, premature, probe.calls());
    }
    ::_exit(valid ? 0 : 34);
  }
  int status = 0;
  ASSERT_EQ(::waitpid(child, &status, 0), child);
  ASSERT_TRUE(WIFEXITED(status)) << "isolated fault child status=" << status;
  EXPECT_EQ(WEXITSTATUS(status), 0);
  ExpectRecording({});
}

TEST_F(McapArenaBoundary, FailedWriteReleasesEveryRemainingBatchRecord) {
  const auto frames = Frames(6, false, 0);
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, false,
                              0, 0, {}, {}, storage);
  ReclaimProbe probe([] { return 0; });
  probe.reclaim.Release();
  Running running(endpoint, storage->append, &probe.reclaim);
  for (const auto& frame : frames) ASSERT_TRUE(Send(endpoint, frame));
  storage->fail_append = true;
  endpoint.Start(nullptr, nullptr);
  endpoint.Stop();
  EXPECT_TRUE(endpoint.finalized());
  EXPECT_TRUE(endpoint.write_failed());
  EXPECT_EQ(endpoint.pending_frames(), 0u);
  EXPECT_EQ(endpoint.pending_bytes(), 0u);
  EXPECT_EQ(endpoint.stats().writes + endpoint.stats().dropped, frames.size());
  EXPECT_GT(endpoint.stats().dropped, 0u);
  EXPECT_EQ(probe.calls(), 4u) << "every excess chunk must be handed back";
  EXPECT_EQ(endpoint.stats().arena_fallbacks, 0u);
}
TEST_F(McapArenaBoundary, ReclaimErrorsPreserveEveryAcceptedFrame) {
  ReclaimWarnings warnings;
  const auto frames = Frames(4, true);
  auto storage = std::make_shared<Storage>();
  McapWriterEndpoint endpoint(path_, resolve_, 0, 0, Policy(), {}, true, 0, 0,
                              {}, {}, storage);
  ReclaimProbe probe([&] { return endpoint.stats().writes; }, EIO);
  Running running(endpoint, storage->append, &probe.reclaim);
  probe.reclaim.Release();
  for (const auto& frame : frames) ASSERT_TRUE(Send(endpoint, frame));
  endpoint.Start(nullptr, nullptr);
  endpoint.Stop();
  EXPECT_GT(probe.calls(), 1u);
  EXPECT_EQ(probe.syscall_result(), -1);
  EXPECT_EQ(reclaim_warnings, 1u);
  ExpectStats(endpoint, frames.size());
  ExpectRecording(frames);
}
}  // namespace
