// PartOutcome: what the storage steps of one part came to (part_outcome.hpp).
#include "part_outcome.hpp"

#include <gtest/gtest.h>

#include <cerrno>
#include <mutex>
#include <string>
#include <vector>

#include "visio_schema/log.hpp"

namespace {
using visio_schema::mcap::IoResult;
using visio_schema::mcap::IoStep;
using visio_schema::mcap::PartOutcome;

std::mutex g_said_mu;
std::vector<std::string> g_said;
void Capture(const visio_schema::log::Record& record) {
  std::lock_guard<std::mutex> lock(g_said_mu);
  g_said.emplace_back(record.text);
}
unsigned Saying(const std::string& words) {
  std::lock_guard<std::mutex> lock(g_said_mu);
  unsigned n = 0;
  for (const auto& line : g_said) n += line.find(words) != std::string::npos;
  return n;
}

class PartOutcomeTest : public ::testing::Test {
 protected:
  void SetUp() override {
    {
      std::lock_guard<std::mutex> lock(g_said_mu);
      g_said.clear();
    }
    visio_schema::log::SetSink(&Capture);
    outcome_.Begin("rec_0001.mcap");
  }
  void TearDown() override { visio_schema::log::SetSink(nullptr); }
  PartOutcome outcome_;
};

// A later failure that may have lost bytes is never hidden by an earlier
// one that did not: completion is told the lossy one, and it is said too.
TEST_F(PartOutcomeTest, ALaterLossyFailureIsNeverHidden) {
  outcome_.Note({IoStep::namespace_sync, EIO, 0, true});
  EXPECT_FALSE(outcome_.lost_bytes());
  EXPECT_EQ(outcome_.result().step, IoStep::namespace_sync);
  outcome_.Note({IoStep::checkpoint, EIO, 0, true});
  EXPECT_TRUE(outcome_.lost_bytes());
  EXPECT_EQ(outcome_.result().step, IoStep::checkpoint);
  outcome_.Note({IoStep::append, EIO, 0, true});
  EXPECT_EQ(outcome_.result().step, IoStep::checkpoint) << "first lossy kept";
  EXPECT_EQ(Saying("directory sync failed"), 1u);
  EXPECT_EQ(Saying("bytes may be lost"), 1u) << "said once";
}

// A failure reported without an errno broke the storage contract: said
// once as such, and taken as EIO.
TEST_F(PartOutcomeTest, AFailureWithoutAnErrnoIsAContractViolation) {
  const IoResult explained = outcome_.Explained({IoStep::append, 0, 7, true});
  EXPECT_EQ(explained.error, EIO);
  EXPECT_EQ(explained.accepted, 7u);
  outcome_.Note({IoStep::flush, 0, 0, true});
  EXPECT_EQ(outcome_.result().error, EIO);
  EXPECT_EQ(Saying("storage contract violation"), 1u);
}

// A part being dropped says nothing as a kept part; a refusal still ends
// the recording, and is said.
TEST_F(PartOutcomeTest, ADroppedPartSaysNothingButARefusal) {
  outcome_.Dropping();
  outcome_.Note({IoStep::close, EIO, 0, true});
  EXPECT_TRUE(outcome_.failed());
  EXPECT_EQ(Saying("rec_0001.mcap"), 0u);
  outcome_.Note({IoStep::close, EROFS, 0, true});
  EXPECT_TRUE(outcome_.refused());
  EXPECT_EQ(Saying("the recording ends"), 1u);
}

// Every part closed unconfirmed is counted, the confirmed ones are not.
TEST_F(PartOutcomeTest, CountsEachUnconfirmedPart) {
  outcome_.Complete({});
  EXPECT_EQ(outcome_.unconfirmed_parts(), 0u);
  outcome_.Complete({IoStep::complete, EIO, 0, true});
  outcome_.Begin("rec_0002.mcap");
  outcome_.Note({IoStep::checkpoint, EIO, 0, true});
  outcome_.Complete(outcome_.result());
  EXPECT_EQ(outcome_.unconfirmed_parts(), 2u);
}
}  // namespace
