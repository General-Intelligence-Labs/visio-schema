// Strict contracts for the real threaded recording endpoint. These expose
// known gaps; a failure is not xfailed, skipped or relabelled as a pass.
#include "mcap_io_fault.hpp"
#include "mcap_writer_test_util.hpp"
#include "visio_schema/mcap/writer_endpoint.hpp"

#include <dirent.h>
#include <fcntl.h>
#include <sys/resource.h>
#include <unistd.h>

#include <filesystem>
#include <csignal>
#include <optional>
#include <string>

#include <gtest/gtest.h>

using mcap_io_test::Fault;
using mcap_io_test::FaultScope;
using visio_schema::mcap::McapWriterEndpoint;

class McapIoContract : public ::testing::TestWithParam<bool> {
 protected:
  void SetUp() override {
    std::string path = (std::filesystem::temp_directory_path() / "mcap_io_XXXXXX").string();
    ASSERT_NE(::mkdtemp(path.data()), nullptr);
    root_ = path;
    channel_ = mcap_test::MakeChannel(16, "/test/imu/0/raw");
  }
  void TearDown() override { std::filesystem::remove_all(root_); }
  std::optional<visio_schema::mcap::RecordingKey> key() const {
    return GetParam() ? std::optional<visio_schema::mcap::RecordingKey>(mcap_test::TestKey(7))
                      : std::nullopt;
  }
  std::unique_ptr<McapWriterEndpoint> endpoint(std::uint64_t rotation = 0) {
    return std::make_unique<McapWriterEndpoint>(
        root_ + "/part.mcap", [this](std::uint32_t id) { return id == 16 ? &channel_ : nullptr; },
        rotation, 0.0, visio_schema::transport::WritePolicy::lossless(),
        std::map<std::string, std::string>{}, false, 0, 32768, key());
  }
  void record(McapWriterEndpoint& ep) {
    ep.Start(nullptr, nullptr);
    for (unsigned i = 0; i < 32; ++i) {
      auto message = mcap_test::Data(16, std::string(32768, 'x'));
      message.seq = i;
      ep.Send(message);
    }
    ep.Stop();
  }
  void expect_failure(Fault kind) {
    auto ep = endpoint();
    FaultScope fault(root_, kind);  // inject after the checked VREC header open
    ASSERT_NO_THROW(record(*ep));
    ASSERT_GT(fault.hits(), 0u) << "positive control: fault never reached a recording fd";
    EXPECT_TRUE(ep->write_failed() || ep->storage_fault())
        << "recording error must reach the health/save outcome";
    EXPECT_EQ(ep->pending_frames(), 0u);
    EXPECT_EQ(ep->pending_bytes(), 0u);
  }
  unsigned held_recording_fds() const {
    unsigned count = 0;
    DIR* dir = ::opendir("/proc/self/fd");
    if (!dir) { ADD_FAILURE() << "cannot inspect fd table"; return 0; }
    while (auto* entry = ::readdir(dir)) {
      char target[4096];
      const std::string link = "/proc/self/fd/" + std::string(entry->d_name);
      const ssize_t n = ::readlink(link.c_str(), target, sizeof(target));
      if (n > 0) {
        const std::string path(target, static_cast<std::size_t>(n));
        if (path == root_ || path.find(root_ + "/") == 0) ++count;
      }
    }
    ::closedir(dir);
    return count;
  }
  std::string root_;
  visio_schema::Channel channel_;
};

INSTANTIATE_TEST_SUITE_P(PlainAndEncrypted, McapIoContract, ::testing::Bool());

TEST_P(McapIoContract, HealthyStopIsReadableAndReleasesDescriptors) {
  auto ep = endpoint();
  record(*ep);
  EXPECT_FALSE(ep->write_failed());
  EXPECT_FALSE(ep->storage_fault());
  EXPECT_EQ(ep->bytes_written(), 32u * 32768u);
  auto bytes = mcap_test::SlurpFile(root_ + "/part.mcap");
  if (key()) bytes = mcap_test::DecryptPart(bytes, *key());
  EXPECT_TRUE(mcap_test::EndsWithMcapMagic(bytes));
  EXPECT_EQ(held_recording_fds(), 0u);
}

TEST_P(McapIoContract, ShortWriteIsReported) { expect_failure(Fault::ShortWrite); }
TEST_P(McapIoContract, WriteErrorIsReported) { expect_failure(Fault::WriteError); }
TEST_P(McapIoContract, FlushErrorIsReported) { expect_failure(Fault::Flush); }
TEST_P(McapIoContract, CloseErrorIsReported) { expect_failure(Fault::Close); }
TEST_P(McapIoContract, CloseMeasurementErrorIsReported) { expect_failure(Fault::CloseMeasure); }
TEST_P(McapIoContract, FileDurabilityErrorIsReported) { expect_failure(Fault::FileSync); }
TEST_P(McapIoContract, DirectoryDurabilityErrorIsReported) { expect_failure(Fault::DirSync); }

TEST_P(McapIoContract, ShortWritesNeverInflateProgressPastActualFileBytes) {
  auto ep = endpoint();
  FaultScope fault(root_, Fault::ShortWrite);
  record(*ep);
  ASSERT_GT(fault.hits(), 0u);
  EXPECT_LE(ep->bytes_written(), std::filesystem::file_size(root_ + "/part.mcap"));
}

TEST_P(McapIoContract, RepeatedRotationAndIdempotentStopLeakNoDescriptors) {
  for (int cycle = 0; cycle < 12; ++cycle) {
    auto ep = endpoint(65536);
    record(*ep);
    ep->Stop();
    EXPECT_FALSE(ep->write_failed()) << "cycle=" << cycle;
    EXPECT_EQ(held_recording_fds(), 0u) << "cycle=" << cycle;
  }
}

TEST_P(McapIoContract, FailedRecordingStillReleasesDescriptors) {
  auto ep = endpoint();
  FaultScope fault(root_, Fault::Close);
  record(*ep);
  ASSERT_GT(fault.hits(), 0u);
  ep->Stop();
  EXPECT_EQ(held_recording_fds(), 0u);
}

TEST_P(McapIoContract, FailedCloseMeasurementStillConsumesTheDescriptor) {
  auto ep = endpoint();
  FaultScope fault(root_, Fault::CloseMeasure);
  record(*ep);
  ASSERT_GT(fault.hits(), 0u);
  EXPECT_TRUE(ep->write_failed());
  EXPECT_EQ(held_recording_fds(), 0u);
}

TEST_P(McapIoContract, LocalClosePreservesMeasurementErrorAndIsIdempotent) {
  visio_schema::mcap::IoResult opened;
  auto file = visio_schema::mcap::LocalWriterStorage()->Create(root_ + "/close.mcap", opened);
  ASSERT_TRUE(file);
  ASSERT_TRUE(file->Append("payload", 7));
  ASSERT_TRUE(file->Checkpoint());
  FaultScope fault(root_, Fault::CloseMeasure);
  const auto closed = file->Close();
  ASSERT_EQ(fault.hits(), 1u);
  EXPECT_EQ(closed.error, EIO);
  EXPECT_EQ(closed.step, visio_schema::mcap::IoStep::close);
  EXPECT_TRUE(closed.uncertain);
  EXPECT_EQ(held_recording_fds(), 0u);
  EXPECT_TRUE(file->Close());
  EXPECT_EQ(fault.hits(), 1u);
}

TEST_P(McapIoContract, RealKernelFileSizeLimitMustReachRecordingHealth) {
  // A real kernel failure, independent of our stdio fault shim. Restrict only
  // the child; neither the parent nor later tests inherit this resource limit.
  EXPECT_EXIT({
    ::signal(SIGXFSZ, SIG_IGN);
    struct rlimit limit{};
    limit.rlim_cur = 65536;
    limit.rlim_max = 65536;
    if (::setrlimit(RLIMIT_FSIZE, &limit) != 0) ::_exit(2);
    auto ep = endpoint();
    record(*ep);
    const auto size = std::filesystem::file_size(root_ + "/part.mcap");
    if (size != 65536) ::_exit(3);  // positive control: actually hit the limit
    ::_exit(ep->write_failed() || ep->storage_fault() ? 0 : 1);
  }, ::testing::ExitedWithCode(0), "");
}
