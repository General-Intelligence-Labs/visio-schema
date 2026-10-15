#include "mcap_writer_test_util.hpp"
#include "visio_schema/mcap/writer.hpp"

#include <cerrno>
#include <cstdlib>
#include <memory>
#include <optional>
#include <vector>

namespace {
using namespace visio_schema::mcap;

// Wrap real storage so fault controls reach MCAP encoding and actual bytes.
class FaultStorage final : public WriterStorage {
 public:
  class File final : public OutputFile {
   public:
    File(FaultStorage& owner, std::unique_ptr<OutputFile> inner)
        : owner_(owner), inner_(std::move(inner)) {}
    IoResult Append(const void* bytes, std::size_t size) override {
      if (owner_.short_next && size > 1) {
        owner_.short_next = false;
        ++owner_.short_hits;
        auto result = inner_->Append(bytes, size / 2);
        result.error = 0;  // Model a backend omitting its short-write error.
        return result;
      }
      return inner_->Append(bytes, size);
    }
    IoResult Flush() override { return inner_->Flush(); }
    IoResult Checkpoint() override { return inner_->Checkpoint(); }
    IoResult Close() override { return inner_->Close(); }
    int Writeback(std::uint64_t off, std::uint64_t len, bool wait) override {
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
    std::unique_ptr<OutputFile> inner_;
  };
  std::unique_ptr<OutputFile> Create(const std::string& path, IoResult& result) override {
    auto file = LocalWriterStorage()->Create(path, result);
    return file ? std::make_unique<File>(*this, std::move(file)) : nullptr;
  }
  IoResult SyncParent(const std::string& path) override {
    return LocalWriterStorage()->SyncParent(path);
  }
  IoResult Remove(const std::string& path) override { return LocalWriterStorage()->Remove(path); }
  IoResult Completed(const std::string&, const IoResult& result) override {
    completed.push_back(result);
    return result;
  }
  bool short_next = false;
  unsigned short_hits = 0;
  int writeback_error = 0;
  bool fail_wait = false;
  unsigned writeback_hits = 0;
  std::vector<IoResult> completed;
};

class McapWriterStorage : public ::testing::TestWithParam<bool> {
 protected:
  void SetUp() override {
    directory_ = mcap_test::TempPath("mcap_storage_contract_XXXXXX");
    ASSERT_NE(::mkdtemp(directory_.data()), nullptr);
    path_ = directory_ + "/part.mcap";
  }
  void TearDown() override { std::filesystem::remove_all(directory_); }
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
  std::string directory_;
  std::string path_;
};
INSTANTIATE_TEST_SUITE_P(PlainAndEncrypted, McapWriterStorage, ::testing::Bool());

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
    EXPECT_EQ(storage->completed.front().error, EIO);
    EXPECT_EQ(storage->completed.front().step, IoStep::checkpoint);
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
}  // namespace
