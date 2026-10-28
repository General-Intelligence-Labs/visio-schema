#include "mcap_writer_test_util.hpp"
#include "visio_schema/log.hpp"
#include "visio_schema/mcap/writer.hpp"
#include "visio_schema/mcap/writer_endpoint.hpp"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <numeric>
#include <cstdlib>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <thread>
#include <vector>

namespace {
using namespace visio_schema::mcap;
using visio_schema::log::Record;
using visio_schema::log::Severity;

// One injected failure: every `step` of part `part` fails with `error`,
// once the first `skip` of them have gone through.
struct InjectedFault {
  IoStep step = IoStep::none;
  int error = 0;
  int part = 0;
  unsigned skip = 0;
  bool and_later = false;  // every later part too
};

// A part's first write is its header (the VREC header, or the MCAP magic):
// failing that is a part that cannot be opened, so a write fault lets it by.
InjectedFault FaultOf(IoStep step, int error, int part) {
  return {step, error, part, step == IoStep::append ? 1u : 0u};
}

// Wrap real storage so fault controls reach MCAP encoding and actual bytes.
class FaultStorage final : public WriterStorage {
 public:
  class File final : public OutputFile {
   public:
    File(FaultStorage& owner, int part, std::unique_ptr<OutputFile> inner)
        : owner_(owner), part_(part), inner_(std::move(inner)) {}
    IoResult Append(const void* bytes, std::size_t size) override {
      if (owner_.short_next && size > 1) {
        owner_.short_next = false;
        ++owner_.short_hits;
        auto result = inner_->Append(bytes, size / 2);
        result.error = 0;  // Model a backend omitting its short-write error.
        return result;
      }
      if (const int error = owner_.Injected(IoStep::append, part_))
        return {IoStep::append, error, 0, true};
      return inner_->Append(bytes, size);
    }
    IoResult Flush() override {
      if (const int error = owner_.Injected(IoStep::flush, part_))
        return {IoStep::flush, error, 0, true};
      return inner_->Flush();
    }
    IoResult Checkpoint() override {
      if (const int error = owner_.Injected(IoStep::checkpoint, part_))
        return {IoStep::checkpoint, error, 0, true};
      return inner_->Checkpoint();
    }
    IoResult Close() override {
      owner_.CloseGate();
      auto closed = inner_->Close();  // the descriptor goes either way
      if (const int error = owner_.Injected(IoStep::close, part_))
        return {IoStep::close, error, closed.accepted, true};
      return closed;
    }
    int Writeback(std::uint64_t off, std::uint64_t len, bool wait) override {
      if (const int error = owner_.Injected(IoStep::writeback, part_))
        return error;
      if (owner_.writeback_error) {
        if (!owner_.fail_wait || wait) {
          ++owner_.writeback_hits;
          return owner_.writeback_error;
        }
        return 0;
      }
      return inner_->Writeback(off, len, wait);
    }
   private:
    FaultStorage& owner_;
    const int part_;
    std::unique_ptr<OutputFile> inner_;
  };
  std::unique_ptr<OutputFile> Create(const std::string& path, IoResult& result) override {
    const int part = opens++;
    if (part >= refuse_open_from || refuse_open_at.count(part)) {
      result = {IoStep::open, refuse_open_error, 0, false};
      return nullptr;
    }
    parts[path] = part;
    auto file = LocalWriterStorage()->Create(path, result);
    if (!file) return nullptr;
    return std::make_unique<File>(*this, part, std::move(file));
  }
  IoResult SyncParent(const std::string& path) override {
    if (sync_error) return {IoStep::namespace_sync, sync_error, 0, true};
    if (const int error = Injected(IoStep::namespace_sync, PartOf(path)))
      return {IoStep::namespace_sync, error, 0, true};
    return LocalWriterStorage()->SyncParent(path);
  }
  IoResult Remove(const std::string& path) override {
    ++removes;
    if (remove_error) return {IoStep::remove, remove_error, 0, true};
    return LocalWriterStorage()->Remove(path);
  }
  // What it answered is what it records.
  IoResult Completed(const std::string& path, const IoResult& result) override {
    IoResult answer = result;
    const int error = result ? Injected(IoStep::complete, PartOf(path)) : 0;
    if (error) answer = {IoStep::complete, error, result.accepted, true};
    completed.push_back(answer);
    completed_by_part[PartOf(path)] = answer;
    return answer;
  }
  int Injected(IoStep step, int part) {
    if (fault.step != step) return 0;
    if (part != fault.part && !(fault.and_later && part > fault.part))
      return 0;
    if (matched_++ < fault.skip) return 0;
    ++fault_hits;
    return fault.error;
  }
  int PartOf(const std::string& path) const {
    const auto it = parts.find(path);
    return it == parts.end() ? -1 : it->second;
  }
  void CloseGate() {
    std::unique_lock<std::mutex> lock(close_mu);
    ++close_calls;
    close_owner = std::this_thread::get_id();
    close_entered = true;
    close_cv.notify_all();
    close_cv.wait(lock, [&] { return !block_close || release_close; });
  }
  bool WaitClose() {
    std::unique_lock<std::mutex> lock(close_mu);
    return close_cv.wait_for(lock, std::chrono::seconds(2), [&] { return close_entered; });
  }
  void ReleaseClose() {
    std::lock_guard<std::mutex> lock(close_mu);
    release_close = true;
    close_cv.notify_all();
  }
  std::mutex close_mu;
  std::condition_variable close_cv;
  bool block_close = false, release_close = false, close_entered = false;
  unsigned close_calls = 0;
  std::thread::id close_owner;
  bool short_next = false;
  unsigned short_hits = 0;
  int writeback_error = 0;
  bool fail_wait = false;
  unsigned writeback_hits = 0;
  InjectedFault fault;
  unsigned matched_ = 0;
  std::atomic<unsigned> fault_hits{0};  // polled while the writer runs
  int refuse_open_from = 1 << 30;
  std::set<int> refuse_open_at;  // these attempts only
  int refuse_open_error = EIO;
  int opens = 0;
  unsigned removes = 0;
  int remove_error = 0;
  int sync_error = 0;  // every directory sync
  std::map<std::string, int> parts;
  std::vector<IoResult> completed;
  std::map<int, IoResult> completed_by_part;
};

// Every line the library says while a test runs.
std::mutex g_lines_mu;
std::vector<std::pair<Severity, std::string>> g_lines;
void CaptureLine(const Record& record) {
  std::lock_guard<std::mutex> lock(g_lines_mu);
  g_lines.emplace_back(record.severity, std::string(record.text));
}
unsigned LinesSaying(const std::string& words) {
  std::lock_guard<std::mutex> lock(g_lines_mu);
  unsigned n = 0;
  for (const auto& line : g_lines)
    if (line.second.find(words) != std::string::npos) ++n;
  return n;
}
constexpr const char* kPartUnconfirmed = "durability not confirmed";
constexpr const char* kRecordingEnds = "the recording ends";

// Part numbers 0..count-1, what a reader expects: a gap is a lost part to
// it (visio_schema Session resets a continuation chain there, and the
// integrity scan reports a missing part).
std::vector<int> Contiguous(std::size_t count) {
  std::vector<int> numbers(count);
  std::iota(numbers.begin(), numbers.end(), 0);
  return numbers;
}

// Polls `done` for up to a second.
template <typename Done>
bool WaitFor(Done done) {
  for (int i = 0; i < 500 && !done(); ++i)
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  return done();
}

class McapWriterStorage : public ::testing::TestWithParam<bool> {
 protected:
  void SetUp() override {
    directory_ = mcap_test::TempPath("mcap_storage_contract_XXXXXX");
    ASSERT_NE(::mkdtemp(directory_.data()), nullptr);
    path_ = directory_ + "/part.mcap";
    ForgetLines();
    visio_schema::log::SetSink(&CaptureLine);
  }
  // A fresh directory and no lines said, for the next case of a loop.
  void Restart() {
    std::filesystem::remove_all(directory_);
    std::filesystem::create_directories(directory_);
    ForgetLines();
  }
  static void ForgetLines() {
    std::lock_guard<std::mutex> lock(g_lines_mu);
    g_lines.clear();
  }
  void TearDown() override {
    visio_schema::log::SetSink(nullptr);
    std::filesystem::remove_all(directory_);
  }
  std::optional<RecordingKey> key() const {
    return GetParam() ? std::optional<RecordingKey>(mcap_test::TestKey(23)) : std::nullopt;
  }
  void write(McapWriter& writer) {
    const auto channel = mcap_test::MakeChannel(16, "/test/imu/0/raw");
    for (unsigned i = 0; i < 32; ++i) {
      auto message = mcap_test::Data(16, std::string(65536, 'x'));
      message.seq = i;
      writer.Write(channel, message);
    }
    writer.Close();
  }
  // A rotating take of `messages` 32 KiB messages, three to a part, with a
  // 1 ms checkpoint and 32 KiB writeback spans, so every step a part has
  // runs in each one.
  std::unique_ptr<McapWriter> rotating(std::shared_ptr<WriterStorage> storage) {
    return std::make_unique<McapWriter>(
        path_, kPartBytes, 0.0, false, 0, 32768, key(), McapReadbackOptions{},
        std::move(storage), std::chrono::milliseconds(1));
  }
  void record(McapWriter& writer, unsigned messages,
              std::size_t bytes = 32768) {
    const auto channel = mcap_test::MakeChannel(16, "/test/imu/0/raw");
    for (unsigned i = 0; i < messages; ++i) {
      auto message = mcap_test::Data(16, std::string(bytes, 'x'));
      message.seq = i;
      writer.Write(channel, message);
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
  }
  std::string part_path(int part) const {
    return mcap_test::PartPath(directory_, "part", part);
  }
  bool part_reads_whole(int part) const {
    return mcap_test::PartIsWhole(part_path(part), key());
  }
  // The part numbers on disk, in order.
  std::vector<int> PartsOnDisk() const {
    std::vector<int> numbers;
    for (const auto& entry : std::filesystem::directory_iterator(directory_)) {
      const auto name = entry.path().filename().string();
      if (name.size() == 14 && name.compare(0, 5, "part_") == 0 &&
          name.compare(9, 5, ".mcap") == 0)
        numbers.push_back(std::stoi(name.substr(5, 4)));
    }
    std::sort(numbers.begin(), numbers.end());
    return numbers;
  }
  void ExpectEveryPartSavedAndWhole(const std::shared_ptr<FaultStorage>& s) {
    for (const int part : PartsOnDisk()) {
      SCOPED_TRACE(part);
      EXPECT_TRUE(part_reads_whole(part));
    }
    for (const auto& completed : s->completed) EXPECT_TRUE(completed);
    EXPECT_EQ(s->completed.size(), PartsOnDisk().size());
  }
  static constexpr std::uint64_t kPartBytes = 3 * 32768;
  static constexpr int kPartOpenAttempts = McapWriter::kPartOpenAttempts;
  std::string directory_;
  std::string path_;
};
INSTANTIATE_TEST_SUITE_P(PlainAndEncrypted, McapWriterStorage, ::testing::Bool());

TEST_P(McapWriterStorage, BoundedCloseRetainsIoOwnerAndAllowsConcurrentStopBudgets) {
  for (bool start : {false, true}) {
    Restart();
    auto storage = std::make_shared<FaultStorage>();
    storage->block_close = true;
    const auto channel = mcap_test::MakeChannel(16, "/test/imu/0/raw");
    McapWriterEndpoint endpoint(path_, [&](std::uint32_t) { return &channel; },
        0, 0.0, visio_schema::transport::WritePolicy::lossless(), {}, false, 0, 0, key(), {}, storage);
    std::thread waiter;
    struct Cleanup {
      std::shared_ptr<FaultStorage> storage;
      std::thread& waiter;
      ~Cleanup() { storage->ReleaseClose(); if (waiter.joinable()) waiter.join(); }
    } cleanup{storage, waiter};
    if (start) endpoint.Start(nullptr, nullptr);
    waiter = std::thread([&] { endpoint.Stop(); });
    ASSERT_TRUE(storage->WaitClose());
    EXPECT_FALSE(endpoint.finalized());
    const auto before = std::chrono::steady_clock::now();
    EXPECT_FALSE(endpoint.StopFor(std::chrono::milliseconds(2)));
    EXPECT_LT(std::chrono::steady_clock::now() - before, std::chrono::milliseconds(500));
    storage->ReleaseClose();
    waiter.join();
    EXPECT_TRUE(endpoint.StopFor(std::chrono::milliseconds(0)));
    EXPECT_TRUE(endpoint.finalized());
    EXPECT_FALSE(endpoint.storage_fault());
    EXPECT_EQ(storage->close_calls, 1u);
    EXPECT_NE(storage->close_owner, std::this_thread::get_id());
    EXPECT_TRUE(mcap_test::PartIsWhole(path_, key()));
  }
}

TEST_P(McapWriterStorage, SynchronizedGroupCutsLossyPartAndKeepsRecording) {
  auto storage = std::make_shared<FaultStorage>();
  storage->fault = FaultOf(IoStep::append, EIO, 0);
  McapRotationPolicy rotation;
  rotation.video_ids = {16, 17};
  rotation.max_staged_bytes = 8 * 1024 * 1024;
  McapWriter writer(path_, 16 * 1024 * 1024, 0, true, 0, 0, key(), {}, storage, {}, rotation);
  for (unsigned id : rotation.video_ids) {
    auto channel = mcap_test::MakeChannel(id, "/camera/" + std::to_string(id));
    channel.schema_name = visio_schema::kCompressedVideoSchema;
    auto message = mcap_test::Data(id, std::string(2 * 1024 * 1024, 'v'));
    message.keyframe = true;
    message.sync_point = {42, 7, 0};
    writer.Write(channel, message);
  }
  EXPECT_EQ(writer.start_status(), McapWriter::StartStatus::Ready);
  EXPECT_FALSE(writer.storage_refused());
  writer.Close();
  ASSERT_GT(storage->fault_hits, 0u);
  ASSERT_EQ(storage->opens, 2) << "loss during aligned flushing must cut before the next record";
  EXPECT_TRUE(std::filesystem::exists(part_path(0)));
  EXPECT_TRUE(part_reads_whole(1));
  EXPECT_EQ(writer.unconfirmed_parts(), 1u);
}

TEST_P(McapWriterStorage, SynchronizedStartReportsFatalStorageRefusal) {
  auto storage = std::make_shared<FaultStorage>();
  storage->fault = FaultOf(IoStep::append, ENOSPC, 0);
  auto channel = mcap_test::MakeChannel(16, "/camera/0");
  channel.schema_name = visio_schema::kCompressedVideoSchema;
  McapRotationPolicy rotation;
  rotation.video_ids = {16};
  rotation.max_staged_bytes = 8 * 1024 * 1024;
  McapWriterEndpoint endpoint(path_, [&](std::uint32_t) { return &channel; },
      16 * 1024 * 1024, 0, visio_schema::transport::WritePolicy::lossless(), {},
      true, 0, 0, key(), {}, storage, {}, rotation);
  endpoint.Start(nullptr, nullptr);
  auto message = mcap_test::Data(16, std::string(2 * 1024 * 1024, 'v'));
  message.keyframe = true;
  message.sync_point = {42, 7, 0};
  endpoint.Send(message);
  ASSERT_TRUE(WaitFor([&] { return endpoint.write_failed(); }));
  EXPECT_FALSE(endpoint.WaitForStart(std::chrono::milliseconds(100)));
  endpoint.Stop();
  EXPECT_GT(storage->fault_hits, 0u);
}

TEST_P(McapWriterStorage, ShortAcceptedBytesCannotPublishSuccessfulCompletion) {
  auto storage = std::make_shared<FaultStorage>();
  McapWriter writer(path_, 0, 0.0, false, 0, 0, key(), {}, storage);
  storage->short_next = true;
  write(writer);
  ASSERT_EQ(storage->short_hits, 1u);
  ASSERT_EQ(storage->completed.size(), 1u);
  EXPECT_TRUE(writer.storage_fault());
  EXPECT_EQ(storage->completed.front().error, EIO);
  EXPECT_TRUE(storage->completed.front().uncertain);
}

#if defined(__linux__)
TEST_P(McapWriterStorage, WritebackMediaFaultPreventsSuccessfulCompletion) {
  for (const bool fail_wait : {false, true}) {
    auto storage = std::make_shared<FaultStorage>();
    storage->writeback_error = EIO;
    storage->fail_wait = fail_wait;
    McapWriter writer(path_, 0, 0.0, false, 0, 32768, key(), {}, storage);
    write(writer);
    ASSERT_GT(storage->writeback_hits, 0u) << "wait=" << fail_wait;
    ASSERT_EQ(storage->completed.size(), 1u);
    EXPECT_TRUE(writer.storage_fault());
    EXPECT_FALSE(writer.storage_refused());
    EXPECT_EQ(storage->completed.front().error, EIO);
    EXPECT_EQ(storage->completed.front().step, IoStep::writeback);
  }
}

TEST_P(McapWriterStorage, UnsupportedWritebackStillFinalizesReadableBytes) {
  for (const auto error : {ENOSYS, EINVAL, EOPNOTSUPP}) {
    auto storage = std::make_shared<FaultStorage>();
    storage->writeback_error = error;
    McapWriter writer(path_, 0, 0.0, false, 0, 32768, key(), {}, storage);
    write(writer);
    ASSERT_GT(storage->writeback_hits, 0u);
    ASSERT_EQ(storage->completed.size(), 1u);
    EXPECT_FALSE(writer.storage_fault()) << "errno=" << error;
    EXPECT_TRUE(storage->completed.front());
    auto bytes = mcap_test::SlurpFile(path_);
    if (key()) bytes = mcap_test::DecryptPart(bytes, *key());
    EXPECT_TRUE(mcap_test::EndsWithMcapMagic(bytes));
  }
}
#endif

// ---- What a failed storage step costs: its part, or the recording ---------

// The steps a part takes, each of which can fail on its own.
std::vector<IoStep> PartSteps() {
  std::vector<IoStep> steps{IoStep::append, IoStep::flush, IoStep::checkpoint,
                            IoStep::close, IoStep::namespace_sync,
                            IoStep::complete};
#if defined(__linux__)
  steps.push_back(IoStep::writeback);  // the bounded writeback is Linux-only
#endif
  return steps;
}

// A failure of any step touches only the part it happened in: the part is
// kept on storage (never removed) with its durability unconfirmed, said
// once and counted, and the recording goes on into parts that save
// normally. A step that loses no bytes leaves the part whole, and storage
// completion is told a lossy step whenever bytes may be lost.
TEST_P(McapWriterStorage, AFailedStepCostsItsPartAndRecordingContinues) {
  for (const IoStep step : PartSteps()) {
    SCOPED_TRACE(static_cast<int>(step));
    Restart();
    auto storage = std::make_shared<FaultStorage>();
    storage->fault = FaultOf(step, EIO, 0);
    auto writer = rotating(storage);
    record(*writer, 12);
    EXPECT_FALSE(writer->storage_refused()) << "the part failed, not storage";
    writer->Close();

    ASSERT_GT(storage->fault_hits, 0u) << "positive control: never injected";
    ASSERT_GE(storage->opens, 3) << "recording did not go on past the part";
    ASSERT_EQ(storage->completed_by_part.count(0), 1u);
    EXPECT_FALSE(storage->completed_by_part[0]) << "unconfirmed, yet saved";
    EXPECT_EQ(LosesBytes(storage->completed_by_part[0].step),
              LosesBytes(step)) << "completion told the wrong kind of loss";
    EXPECT_TRUE(std::filesystem::exists(part_path(0))) << "part not kept";
    EXPECT_EQ(storage->removes, 0u);
    if (!LosesBytes(step)) EXPECT_TRUE(part_reads_whole(0));
    for (int part = 1; part < storage->opens; ++part) {
      ASSERT_EQ(storage->completed_by_part.count(part), 1u) << part;
      EXPECT_TRUE(storage->completed_by_part[part]) << part;
      EXPECT_TRUE(part_reads_whole(part)) << part;
    }
    EXPECT_EQ(writer->unconfirmed_parts(), 1u);
    EXPECT_TRUE(writer->storage_fault());
    EXPECT_FALSE(writer->storage_refused());
    EXPECT_EQ(LinesSaying(kPartUnconfirmed), 1u) << "said once for the part";
    EXPECT_EQ(LinesSaying(part_path(0)), 1u);
    EXPECT_EQ(LinesSaying(kRecordingEnds), 0u);
  }
}

// A step that may have lost bytes leaves the part's offsets (and an
// encrypted part's keystream position) out of step with its file: the part
// is cut at its next record and kept, and the part after it decodes (and
// decrypts) whole. A close loses bytes only as the part ends anyway.
// Records of 256 KiB fill a chunk every few, so bytes reach storage, and
// fail there, while the part is still being written.
TEST_P(McapWriterStorage, ALossyFailureCutsThePartAtItsNextRecord) {
  constexpr std::size_t kRecord = 256 * 1024;
  const auto take = [&](std::shared_ptr<FaultStorage> storage) {
    McapWriter writer(path_, 8 * kRecord, 0.0, false, 0, 32768, key(),
                      McapReadbackOptions{}, std::move(storage),
                      std::chrono::milliseconds(1));
    record(writer, 24, kRecord);
    writer.Close();
  };
  auto clean = std::make_shared<FaultStorage>();
  take(clean);
  const int uncut = clean->opens;
  for (const IoStep step : PartSteps()) {
    if (!LosesBytes(step) || step == IoStep::close) continue;
    SCOPED_TRACE(static_cast<int>(step));
    Restart();
    auto storage = std::make_shared<FaultStorage>();
    storage->fault = FaultOf(step, EIO, 0);
    take(storage);
    ASSERT_GT(storage->fault_hits, 0u) << "positive control: never injected";
    EXPECT_GT(storage->opens, uncut) << "the part was not cut early";
    EXPECT_TRUE(std::filesystem::exists(part_path(0))) << "part not kept";
    for (int part = 1; part < storage->opens; ++part)
      EXPECT_TRUE(part_reads_whole(part)) << part;
    EXPECT_EQ(PartsOnDisk(), Contiguous(storage->opens));
  }
}

// A write the storage short-changed without saying why broke its
// contract: said once as such, and taken as a lossy EIO.
TEST_P(McapWriterStorage, AShortWriteWithoutAnErrnoIsAContractViolation) {
  auto storage = std::make_shared<FaultStorage>();
  McapWriter writer(path_, 0, 0.0, false, 0, 0, key(), {}, storage);
  storage->short_next = true;
  write(writer);
  ASSERT_EQ(storage->short_hits, 1u);
  ASSERT_EQ(storage->completed.size(), 1u);
  EXPECT_EQ(storage->completed.front().error, EIO);
  EXPECT_TRUE(LosesBytes(storage->completed.front().step));
  EXPECT_EQ(LinesSaying("storage contract violation"), 1u);
}

// A part whose only failure lost no bytes still never claims more bytes
// written than its file holds.
TEST_P(McapWriterStorage, BytesWrittenNeverExceedTheFileForAnUnconfirmedPart) {
  auto storage = std::make_shared<FaultStorage>();
  storage->fault = FaultOf(IoStep::namespace_sync, EIO, 0);
  McapWriter writer(path_, 0, 0.0, false, 0, 0, key(), {}, storage);
  write(writer);
  EXPECT_EQ(writer.unconfirmed_parts(), 1u);
  EXPECT_LE(writer.bytes_written(), std::filesystem::file_size(path_));
}

// The failures no later part could escape end the recording, whichever step
// meets them: the writer writes nothing more and opens no further part.
TEST_P(McapWriterStorage, AVolumeThatRefusesWritesEndsTheRecording) {
  std::vector<InjectedFault> faults;
  for (const int error : {EROFS, ENOSPC, ENODEV})
    for (const IoStep step : PartSteps())
      faults.push_back(FaultOf(step, error, 0));
  for (const InjectedFault& fault : faults) {
    SCOPED_TRACE(std::to_string(static_cast<int>(fault.step)) + " " +
                 std::strerror(fault.error));
    Restart();
    auto storage = std::make_shared<FaultStorage>();
    storage->fault = fault;
    auto writer = rotating(storage);
    record(*writer, 12);
    EXPECT_TRUE(writer->storage_refused());
    const auto written = writer->bytes_written();
    record(*writer, 3);
    EXPECT_EQ(writer->bytes_written(), written) << "wrote past a refusal";
    writer->Close();

    ASSERT_GT(storage->fault_hits, 0u) << "positive control: never injected";
    EXPECT_EQ(storage->opens, 1) << "a part opened after the refusal";
    EXPECT_FALSE(std::filesystem::exists(part_path(1)));
    ASSERT_EQ(storage->completed.size(), 1u) << "part 0 completed once";
    EXPECT_FALSE(storage->completed_by_part[0]);
    EXPECT_TRUE(std::filesystem::exists(part_path(0))) << "part not kept";
    EXPECT_EQ(storage->removes, 0u);
    EXPECT_EQ(writer->unconfirmed_parts(), 1u);
    EXPECT_TRUE(writer->storage_fault());
    EXPECT_EQ(LinesSaying(kRecordingEnds), 1u) << "said once";
    EXPECT_EQ(LinesSaying(kPartUnconfirmed), 0u);
  }
}

// A part that saved can be followed by one that cannot: the refusal ends
// the recording there, and what was saved before it stays saved.
TEST_P(McapWriterStorage, ARefusalInALaterPartKeepsTheSavedOnes) {
  auto storage = std::make_shared<FaultStorage>();
  storage->fault = FaultOf(IoStep::checkpoint, ENOSPC, 1);
  auto writer = rotating(storage);
  record(*writer, 12);
  writer->Close();
  EXPECT_TRUE(writer->storage_refused());
  EXPECT_EQ(storage->opens, 2);
  EXPECT_TRUE(storage->completed_by_part[0]);
  EXPECT_TRUE(part_reads_whole(0));
  EXPECT_FALSE(storage->completed_by_part[1]);
}

// ---- A part that will not open ---------------------------------------------

// Every attempt failing ends the recording: tried kPartOpenAttempts times
// on a doubling wait, said once, thrown once; the recording then writes
// nothing more and opens nothing more, and the parts it never opened are
// never removed or completed by the Close() that follows.
TEST_P(McapWriterStorage, ANextPartThatNeverOpensEndsTheRecordingAfterRetries) {
  auto storage = std::make_shared<FaultStorage>();
  storage->refuse_open_from = 1;
  storage->refuse_open_error = EIO;
  auto writer = rotating(storage);
  const auto began = std::chrono::steady_clock::now();
  EXPECT_THROW(record(*writer, 12), std::runtime_error);
  const auto took = std::chrono::steady_clock::now() - began;
  EXPECT_GE(took, McapWriter::kPartOpenFirstWait * 7) << "no doubling wait";
  EXPECT_TRUE(writer->storage_refused()) << "the end is not latched";
  EXPECT_EQ(LinesSaying("trying"), 1u) << "said once for the part";
  EXPECT_NO_THROW(record(*writer, 3));
  writer->Close();
  EXPECT_EQ(storage->opens, 1 + kPartOpenAttempts) << "opened after the end";
  EXPECT_EQ(writer->part_open_retries(),
            static_cast<std::uint64_t>(kPartOpenAttempts - 1));
  ASSERT_EQ(storage->completed.size(), 1u) << "only part 0 completes";
  EXPECT_TRUE(storage->completed.front());
  EXPECT_EQ(storage->removes, 0u);
  EXPECT_TRUE(part_reads_whole(0));
  EXPECT_EQ(PartsOnDisk(), std::vector<int>{0});
}

// The first part of a recording follows the same rule: tried again, and
// the writer is never built when every attempt fails.
TEST_P(McapWriterStorage, TheFirstPartIsTriedAgainOrTheWriterIsNeverBuilt) {
  auto storage = std::make_shared<FaultStorage>();
  storage->refuse_open_from = 0;
  EXPECT_THROW(rotating(storage), std::runtime_error);
  EXPECT_EQ(storage->opens, kPartOpenAttempts);
  EXPECT_TRUE(PartsOnDisk().empty());
  Restart();
  storage = std::make_shared<FaultStorage>();
  storage->refuse_open_from = 0;
  storage->refuse_open_error = EROFS;
  EXPECT_THROW(rotating(storage), std::runtime_error);
  EXPECT_EQ(storage->opens, 1) << "a refusal was tried again";
}

// A failure no part could escape is not tried again, and is latched as the
// volume's refusal; the part before it stays kept and whole.
TEST_P(McapWriterStorage, ANextPartRefusedForGoodIsNotTriedAgain) {
  for (const int error : {EROFS, ENOSPC, ENODEV}) {
    SCOPED_TRACE(std::strerror(error));
    Restart();
    auto storage = std::make_shared<FaultStorage>();
    storage->refuse_open_from = 1;
    storage->refuse_open_error = error;
    auto writer = rotating(storage);
    EXPECT_THROW(record(*writer, 12), std::runtime_error);
    EXPECT_TRUE(writer->storage_refused());
    EXPECT_EQ(LinesSaying(kRecordingEnds), 1u) << "the refusal is unsaid";
    writer->Close();
    EXPECT_EQ(storage->opens, 2);
    EXPECT_EQ(writer->part_open_retries(), 0u);
    EXPECT_EQ(storage->completed.size(), 1u);
    EXPECT_EQ(storage->removes, 0u);
    EXPECT_EQ(PartsOnDisk(), std::vector<int>{0});
  }
}

// A part that will not open for a reason storage may get past is tried
// again under its own number, the first part of a recording as much as a
// later one: the recording goes on, its parts numbered without a gap, the
// failure said once and counted.
TEST_P(McapWriterStorage, AnOpenThatFailsOnceIsTriedAgainAsAFreshPart) {
  for (const int attempt : {0, 1}) {
    SCOPED_TRACE(attempt);
    Restart();
    auto storage = std::make_shared<FaultStorage>();
    storage->refuse_open_at = {attempt};
    auto writer = rotating(storage);
    record(*writer, 12);
    writer->Close();
    EXPECT_FALSE(writer->storage_refused());
    EXPECT_EQ(writer->part_open_retries(), 1u);
    EXPECT_EQ(LinesSaying("trying"), 1u) << "said once";
    EXPECT_EQ(writer->unconfirmed_parts(), 0u);
    EXPECT_GE(storage->opens, 5) << "not every part was opened";
    ExpectEveryPartSavedAndWhole(storage);
    EXPECT_EQ(PartsOnDisk(), Contiguous(PartsOnDisk().size()));
  }
}

// A name another file already holds is never opened over: the real
// exclusive local storage refuses it, and the retry moves at once to the
// next number (the one gap a reader may see, and the log says why),
// costing no attempt -- here more taken names follow than a part has
// attempts. The files under the taken names are left as they were.
TEST_P(McapWriterStorage, ANameAlreadyTakenIsSkippedAndLeftIntact) {
  const std::string sentinel(1234, 's');
  for (int part = 1; part <= kPartOpenAttempts; ++part)
    std::ofstream(part_path(part), std::ios::binary) << sentinel;
  auto writer = rotating(LocalWriterStorage(true));
  record(*writer, 12);
  writer->Close();
  EXPECT_FALSE(writer->storage_refused()) << "a taken name cost attempts";
  EXPECT_EQ(writer->part_open_retries(),
            static_cast<std::uint64_t>(kPartOpenAttempts));
  const int next = kPartOpenAttempts + 1;
  ASSERT_GT(PartsOnDisk().size(), static_cast<std::size_t>(next));
  EXPECT_EQ(PartsOnDisk()[1], 1);
  EXPECT_EQ(std::filesystem::file_size(part_path(1)), sentinel.size())
      << "the taken name was opened over";
  EXPECT_EQ(mcap_test::SlurpFile(part_path(1)), sentinel);
  EXPECT_EQ(PartsOnDisk()[next], next) << "the part did not move on";
  EXPECT_TRUE(part_reads_whole(0));
  EXPECT_TRUE(part_reads_whole(next));
}

// A recording that does not rotate has no next name: a taken one ends it
// at once, never tried again under the same path.
TEST_P(McapWriterStorage, ATakenNameEndsAWriterThatDoesNotRotate) {
  {
    std::ofstream(path_, std::ios::binary) << "taken";
  }
  const auto began = std::chrono::steady_clock::now();
  EXPECT_THROW(McapWriter(path_, 0, 0.0, false, 0, 0, key(), {},
                          LocalWriterStorage(true)),
               std::runtime_error);
  EXPECT_LT(std::chrono::steady_clock::now() - began,
            McapWriter::kPartOpenFirstWait) << "it waited to try again";
  EXPECT_EQ(mcap_test::SlurpFile(path_), "taken");
}

// A part whose header the storage refuses never opened and holds no
// recorded data: its file is removed, and the recording goes on under the
// same number.
TEST_P(McapWriterStorage, AFailedHeaderRemovesTheEmptyPartAndGoesOn) {
  if (!key()) GTEST_SKIP() << "a plaintext part has no header of its own";
  auto storage = std::make_shared<FaultStorage>();
  storage->fault = {IoStep::append, EIO, 1, 0};
  auto writer = rotating(storage);
  record(*writer, 12);
  writer->Close();
  EXPECT_FALSE(writer->storage_refused());
  EXPECT_EQ(storage->removes, 1u);
  EXPECT_EQ(writer->part_open_retries(), 1u);
  EXPECT_EQ(writer->unconfirmed_parts(), 0u);
  ExpectEveryPartSavedAndWhole(storage);
  EXPECT_EQ(PartsOnDisk(), Contiguous(PartsOnDisk().size()));
}

// A plaintext part has no header of its own: its first write is the MCAP
// stream's, and a failure there is the part's -- kept, unconfirmed, cut.
TEST_P(McapWriterStorage, APlaintextPartsFirstWriteFailureIsThePartsOwn) {
  if (key()) GTEST_SKIP() << "an encrypted part opens with its header";
  auto storage = std::make_shared<FaultStorage>();
  storage->fault = {IoStep::append, EIO, 1, 0};
  auto writer = rotating(storage);
  record(*writer, 12);
  writer->Close();
  EXPECT_EQ(writer->unconfirmed_parts(), 1u);
  EXPECT_EQ(storage->removes, 0u);
  EXPECT_TRUE(std::filesystem::exists(part_path(1)));
}

// A header the volume refuses for good: the empty file is removed, nothing
// is tried again, and the recording ends with part 0 kept.
TEST_P(McapWriterStorage, AHeaderRefusedForGoodEndsTheRecording) {
  if (!key()) GTEST_SKIP() << "a plaintext part has no header of its own";
  auto storage = std::make_shared<FaultStorage>();
  storage->fault = {IoStep::append, ENOSPC, 1, 0};
  auto writer = rotating(storage);
  EXPECT_THROW(record(*writer, 12), std::runtime_error);
  EXPECT_TRUE(writer->storage_refused());
  writer->Close();
  EXPECT_EQ(storage->removes, 1u);
  EXPECT_EQ(writer->part_open_retries(), 0u);
  EXPECT_EQ(PartsOnDisk(), std::vector<int>{0});
}

// A failed header whose file cannot be removed leaves its name taken: the
// retry moves on to the next number rather than open over it, and the drop
// is said as such.
TEST_P(McapWriterStorage, AnUnremovableFailedPartIsSkippedNotReused) {
  if (!key()) GTEST_SKIP() << "a plaintext part has no header of its own";
  auto storage = std::make_shared<FaultStorage>();
  storage->fault = {IoStep::append, EIO, 1, 0};
  storage->remove_error = EIO;
  auto writer = rotating(storage);
  record(*writer, 12);
  writer->Close();
  EXPECT_FALSE(writer->storage_refused());
  EXPECT_EQ(writer->part_open_retries(), 1u);
  EXPECT_EQ(storage->removes, 1u);
  EXPECT_TRUE(writer->storage_fault()) << "an undropped part is uncounted";
  EXPECT_EQ(LinesSaying("could not drop empty part"), 1u);
  ASSERT_GE(PartsOnDisk().size(), 3u);
  EXPECT_EQ(PartsOnDisk()[1], 1) << "the failed file is still there";
  EXPECT_EQ(std::filesystem::file_size(part_path(1)), 0u)
      << "the failed file was opened over";  // its header never landed
  EXPECT_EQ(PartsOnDisk()[2], 2) << "the next number carries the part";
}

// Only the last attempt makes a failed part's removal durable (one that
// follows re-creates the name); when that sync fails the removal still
// stands, and the failure is said as the drop's.
TEST_P(McapWriterStorage, ARemovalWhoseSyncFailsIsSaidAndStillStands) {
  if (!key()) GTEST_SKIP() << "a plaintext part has no header of its own";
  auto storage = std::make_shared<FaultStorage>();
  storage->fault = {IoStep::append, EIO, 1, 0, true};
  storage->sync_error = EIO;
  auto writer = rotating(storage);
  EXPECT_THROW(record(*writer, 12), std::runtime_error);
  EXPECT_EQ(LinesSaying("could not drop empty part"), 1u)
      << "only the last attempt syncs its removal";
  writer->Close();
  EXPECT_EQ(PartsOnDisk(), std::vector<int>{0});
}

// The endpoint a device records through: a part-level failure never sets
// write_failed() (the owner's cue to end the take); a refusal does, and
// what arrives after it is shed and counted.
TEST_P(McapWriterStorage, OnlyARefusalTellsTheOwnerToEndTheTake) {
  for (const int error : {EIO, EROFS}) {
    SCOPED_TRACE(std::strerror(error));
    Restart();
    auto storage = std::make_shared<FaultStorage>();
    storage->fault = FaultOf(IoStep::checkpoint, error, 0);
    const auto channel = mcap_test::MakeChannel(16, "/test/imu/0/raw");
    McapWriterEndpoint ep(
        path_, [&](std::uint32_t) { return &channel; }, kPartBytes, 0.0,
        visio_schema::transport::WritePolicy::lossless(), {}, false, 0, 32768,
        key(), {}, storage, std::chrono::milliseconds(1));
    ep.Start(nullptr, nullptr);
    for (unsigned i = 0; i < 12; ++i) {
      ep.Send(mcap_test::Data(16, std::string(32768, 'x')));
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    ASSERT_TRUE(WaitFor([&] { return storage->fault_hits > 0; }));
    const bool refused = error == EROFS;
    EXPECT_TRUE(WaitFor([&] { return ep.write_failed() == refused; }));
    ep.Stop();
    EXPECT_EQ(ep.write_failed(), refused) << "Stop changed the verdict";
    EXPECT_TRUE(ep.storage_fault());
    EXPECT_EQ(ep.stats().unconfirmed_parts, 1u);
    if (refused) EXPECT_GT(ep.stats().dropped, 0u) << "shed uncounted";
    if (!refused) EXPECT_GE(storage->opens, 3);
  }
}

// The endpoint passes the writer's open retries through, and sheds (and
// counts) what was queued once no part could be opened.
TEST_P(McapWriterStorage, TheEndpointCountsRetriesAndShedsAfterTheEnd) {
  auto storage = std::make_shared<FaultStorage>();
  storage->refuse_open_from = 1;
  const auto channel = mcap_test::MakeChannel(16, "/test/imu/0/raw");
  McapWriterEndpoint ep(
      path_, [&](std::uint32_t) { return &channel; }, kPartBytes, 0.0,
      visio_schema::transport::WritePolicy::lossless(), {}, false, 0, 32768,
      key(), {}, storage, std::chrono::milliseconds(1));
  ep.Start(nullptr, nullptr);
  for (unsigned i = 0; i < 12; ++i)
    ep.Send(mcap_test::Data(16, std::string(32768, 'x')));
  EXPECT_TRUE(WaitFor([&] { return ep.write_failed(); }));
  ep.Stop();
  EXPECT_EQ(ep.stats().part_open_retries,
            static_cast<std::uint64_t>(kPartOpenAttempts - 1));
  EXPECT_GT(ep.stats().dropped, 0u) << "frames after the end uncounted";
  EXPECT_EQ(ep.stats().dropped + 3, 12u) << "every unwritten frame counted";
}
}  // namespace
