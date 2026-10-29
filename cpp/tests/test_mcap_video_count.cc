// Count accepted video messages independently of enqueue/capture/attempts.
#include "visio_schema/mcap/writer.hpp"
#include "visio_schema/mcap/writer_endpoint.hpp"

#include <gtest/gtest.h>

#include <cerrno>
#include <chrono>
#include <filesystem>
#include <memory>
#include <string>
#include <utility>

#define MCAP_COMPRESSION_NO_LZ4
#define MCAP_COMPRESSION_NO_ZSTD
#define MCAP_IMPLEMENTATION
// The test reader must not share upstream's non-inline symbols with the writer.
#define mcap video_count_mcap
#include <mcap/reader.hpp>
#undef mcap
#undef MCAP_IMPLEMENTATION

#include "mcap_writer_test_util.hpp"
#include "visio_schema/wire/time.hpp"

namespace {
using visio_schema::Channel;
using namespace visio_schema::mcap;
using visio_schema::transport::WritePolicy;
using visio_schema::wire::Message;
namespace fs = std::filesystem;

Channel Video(std::uint32_t id) {
  auto channel = mcap_test::MakeChannel(id, "/camera/" + std::to_string(id));
  channel.schema_name = "foxglove.CompressedVideo";
  return channel;
}

Message Frame(std::uint32_t id, bool keyframe, std::uint32_t seq = 1,
              std::size_t size = 4) {
  auto message = mcap_test::Data(id, std::string(size, 'v'));
  message.seq = seq;
  message.keyframe = keyframe;
  message.sync_point = {1, 1, seq};
  visio_schema::SetTimestampNs(&message.timestamp, 1'000'000 * seq);
  return message;
}

class McapVideoCount : public testing::Test {
protected:
  void SetUp() override {
    const auto suffix = std::to_string(
        std::chrono::steady_clock::now().time_since_epoch().count());
    dir_ = mcap_test::TempPath("visio-video-count-" + suffix);
    ASSERT_TRUE(fs::create_directory(dir_));
    path_ = dir_ + "/take.mcap";
  }
  void TearDown() override { fs::remove_all(dir_); }

  std::uint64_t ReadVideoMessages() const {
    std::uint64_t count = 0;
    for (const auto &file : fs::directory_iterator(dir_)) {
      if (file.path().extension() != ".mcap")
        continue;
      ::video_count_mcap::McapReader reader;
      const auto opened = reader.open(file.path().string());
      EXPECT_TRUE(opened.ok()) << opened.message;
      if (!opened.ok())
        continue;
      const auto on_error = [](const auto &status) {
        ADD_FAILURE() << status.message;
      };
      for (const auto &row : reader.readMessages(on_error))
        if (row.schema && row.schema->name == "foxglove.CompressedVideo")
          ++count;
    }
    return count;
  }
  std::string dir_, path_;
};

TEST_F(McapVideoCount, ZeroVideoKeepsANonVideoTakeAndCountsNoPreIdrFrames) {
  const auto video = Video(16);
  const auto imu = mcap_test::MakeChannel(17, "/imu/0/raw");
  McapWriter writer(path_);
  EXPECT_EQ(writer.video_frames_written(), 0u);
  writer.Write(imu, mcap_test::Data(17, "imu"));
  writer.Write(video, Frame(16, false));
  writer.Write(video, Frame(16, false, 2));
  writer.Close();
  ASSERT_TRUE(fs::exists(path_)) << "a zero-video take must be retained";
  EXPECT_EQ(writer.video_frames_written(), 0u);
  EXPECT_EQ(ReadVideoMessages(), 0u);
}

TEST_F(McapVideoCount, CountsMessagesFromEveryCameraButNoOtherSchema) {
  const auto left = Video(16), right = Video(17);
  // A camera-looking topic alone does not make an IMU record a video frame.
  const auto imu = mcap_test::MakeChannel(18, "/camera/imu");
  auto audio = mcap_test::MakeChannel(19, "/audio/0");
  audio.schema_name = "foxglove.RawAudio";
  McapWriter writer(path_);
  writer.Write(left, Frame(16, false));
  writer.Write(imu, mcap_test::Data(18, "imu"));
  writer.Write(audio, mcap_test::Data(19, "audio"));
  writer.Write(left, Frame(16, true));
  writer.Write(right, Frame(17, false));
  writer.Write(left, Frame(16, false, 2));
  writer.Write(right, Frame(17, true, 2));
  writer.Write(right, Frame(17, false, 3));
  EXPECT_EQ(writer.video_frames_written(), 4u);
  writer.Close();
  EXPECT_EQ(ReadVideoMessages(), 4u);
  writer.Write(left, Frame(16, true, 4));
  writer.Close();
  EXPECT_EQ(writer.video_frames_written(), 4u);
}

TEST_F(McapVideoCount, CountSurvivesRollsAndExcludesTheRotationGate) {
  const auto video = Video(16);
  McapWriter writer(path_, 4);
  writer.Write(video, Frame(16, true));
  writer.Write(video, Frame(16, false, 2)); // rolls, then drops before IDR
  EXPECT_EQ(writer.keyframe_gated(), 1u);
  EXPECT_EQ(writer.video_frames_written(), 1u);
  writer.Write(video, Frame(16, true, 3));
  writer.Write(video, Frame(16, true, 4));
  writer.Close();
  EXPECT_EQ(writer.video_frames_written(), 3u);
  EXPECT_EQ(ReadVideoMessages(), 3u);
  EXPECT_TRUE(fs::exists(dir_ + "/take_0002.mcap"));
}

TEST_F(McapVideoCount, CloseIncludesAnIncompleteRotationCandidate) {
  const auto left = Video(16), right = Video(17);
  McapRotationPolicy rotation;
  rotation.video_ids = {16, 17};
  rotation.start_wait = std::chrono::seconds(30);
  rotation.max_wait = std::chrono::seconds(30);
  McapWriter writer(path_, 1024, 0, true, 0, 0, {}, {}, {}, {}, rotation);
  writer.Write(left, Frame(16, true, 1, 512));
  EXPECT_EQ(writer.video_frames_written(), 0u) << "staged is not written";
  writer.Write(right, Frame(17, true, 1, 512));
  EXPECT_EQ(writer.video_frames_written(), 2u);
  writer.Write(left, Frame(16, true, 2));
  EXPECT_GT(writer.rotation_pending_bytes(), 0u);
  EXPECT_EQ(writer.video_frames_written(), 2u);
  writer.Close();
  EXPECT_EQ(writer.video_frames_written(), 3u);
  EXPECT_EQ(ReadVideoMessages(), 3u);
}

TEST_F(McapVideoCount, IncompleteStartNeverCountsItsStagedVideo) {
  const auto left = Video(16);
  McapRotationPolicy rotation;
  rotation.video_ids = {16, 17};
  rotation.start_wait = std::chrono::seconds(30);
  McapWriter writer(path_, 1024, 0, true, 0, 0, {}, {}, {}, {}, rotation);
  writer.Write(left, Frame(16, true));
  EXPECT_GT(writer.rotation_pending_bytes(), 0u);
  writer.Close();
  EXPECT_EQ(writer.video_frames_written(), 0u);
  EXPECT_EQ(ReadVideoMessages(), 0u);
}

TEST_F(McapVideoCount, EndpointDrainsOnlyMappedAndRetainedQueueEntries) {
  const auto video = Video(16);
  McapWriterEndpoint endpoint(
      path_, [&](std::uint32_t id) { return id == 16 ? &video : nullptr; }, 0,
      0, WritePolicy::drop_on_fail(2));
  // Queue BEFORE the worker exists: exact shedding, independent of timing.
  for (std::uint32_t i = 1; i <= 4; ++i)
    endpoint.Send(Frame(16, true, i));
  endpoint.Send(Frame(99, true));
  EXPECT_EQ(endpoint.stats().video_frames, 0u);
  EXPECT_EQ(endpoint.stats().dropped, 2u);
  EXPECT_EQ(endpoint.stats().unmapped, 1u);
  endpoint.Stop(); // also starts the drain worker for a prepared endpoint
  EXPECT_EQ(endpoint.pending_frames(), 0u);
  EXPECT_EQ(endpoint.stats().writes, 2u);
  EXPECT_EQ(endpoint.stats().video_frames, 2u);
  EXPECT_EQ(ReadVideoMessages(), 2u);
  endpoint.Stop();
  EXPECT_EQ(endpoint.stats().video_frames, 2u);
}

// Upstream MCAP's status can be OK even when its backing file failed. Wrap
// real storage to exercise that failure without depending on a full volume.
class FailedAppendStorage : public WriterStorage {
public:
  class File : public OutputFile {
  public:
    File(FailedAppendStorage &owner, std::unique_ptr<OutputFile> inner)
        : owner_(owner), inner_(std::move(inner)) {}
    IoResult Append(const void *data, std::size_t size) override {
      if (!owner_.error)
        return inner_->Append(data, size);
      ++owner_.hits;
      return {IoStep::append, owner_.error, 0, true};
    }
    IoResult Flush() override { return inner_->Flush(); }
    IoResult Checkpoint() override { return inner_->Checkpoint(); }
    IoResult Close() override { return inner_->Close(); }
    int Writeback(std::uint64_t off, std::uint64_t len, bool wait) override {
      return inner_->Writeback(off, len, wait);
    }

  private:
    FailedAppendStorage &owner_;
    std::unique_ptr<OutputFile> inner_;
  };
  std::unique_ptr<OutputFile> Create(const std::string &path,
                                     IoResult &result) override {
    auto file = inner_->Create(path, result);
    return file ? std::make_unique<File>(*this, std::move(file)) : nullptr;
  }
  IoResult SyncParent(const std::string &path) override {
    return inner_->SyncParent(path);
  }
  IoResult Remove(const std::string &path) override {
    return inner_->Remove(path);
  }
  int error = 0;
  unsigned hits = 0;

private:
  std::shared_ptr<WriterStorage> inner_ = LocalWriterStorage();
};

TEST_F(McapVideoCount, LaterFlushFailureDoesNotProveAcceptedVideoWasRetained) {
  const auto video = Video(16);
  auto storage = std::make_shared<FailedAppendStorage>();
  McapWriter writer(path_, 0, 0, false, 0, 0, {}, {}, storage);
  writer.Write(video, Frame(16, true)); // still in the MCAP chunk buffer
  ASSERT_EQ(writer.video_frames_written(), 1u);
  storage->error = EIO;
  writer.Close();
  ASSERT_GT(storage->hits, 0u);
  EXPECT_TRUE(writer.storage_fault());
  // Accepted-message telemetry alone cannot establish retained media content.
  // The firmware must report an unknown final count after this failed close.
  EXPECT_EQ(writer.video_frames_written(), 1u);
}

TEST_F(McapVideoCount, AStorageWriteFailureCannotCountAsSuccessfulVideo) {
  const auto video = Video(16);
  for (const int error : {EIO, ENOSPC}) {
    SCOPED_TRACE(error);
    auto storage = std::make_shared<FailedAppendStorage>();
    McapWriter writer(path_, 0, 0, false, 0, 0, {}, {}, storage);
    storage->error = error;
    // Larger than one MCAP chunk: the call must reach storage before return.
    writer.Write(video, Frame(16, true, 1, 2 * 1024 * 1024));
    ASSERT_GT(storage->hits, 0u);
    EXPECT_EQ(writer.video_frames_written(), 0u);
    writer.Close();
    EXPECT_EQ(writer.video_frames_written(), 0u);
    EXPECT_TRUE(writer.storage_fault());
    fs::remove(path_);
  }
}
} // namespace
