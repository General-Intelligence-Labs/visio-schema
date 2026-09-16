// Where the library's runtime lines go (log.hpp).
//
// An application that installs a sink is trusting that EVERY line reaches it,
// with the severity the reporting site chose, and that nothing still leaks to
// stderr beside it — a device log missing the one line that explains a failed
// recording is the failure this pins against. The default is pinned too: a
// program that installs nothing must still see each line, whole.
#include <gtest/gtest.h>

#include <cstddef>
#include <string>
#include <vector>

#include "visio_schema/log.hpp"

namespace {

using visio_schema::log::kCutMarker;
using visio_schema::log::kMaxLineBytes;
using visio_schema::log::Record;
using visio_schema::log::SetSink;
using visio_schema::log::Severity;
using visio_schema::log::Write;

struct Delivered {
  Severity severity;
  const char* format;
  std::string text;
  bool nul_terminated;
};

std::vector<Delivered>& Captured() {
  static std::vector<Delivered> lines;
  return lines;
}

void CapturingSink(const Record& record) {
  Captured().push_back({record.severity, record.format,
                        std::string(record.text),
                        record.text.data()[record.text.size()] == '\0'});
}

class LogTest : public ::testing::Test {
 protected:
  void SetUp() override { Captured().clear(); }
  void TearDown() override { SetSink(nullptr); }
};

TEST_F(LogTest, WithNoSinkEachLineIsOneNewlineTerminatedStderrLine) {
  testing::internal::CaptureStderr();
  Write(Severity::kWarning, "link stalled (no reader for %d ms)", 250);
  EXPECT_EQ(testing::internal::GetCapturedStderr(),
            "link stalled (no reader for 250 ms)\n");
}

TEST_F(LogTest, AnInstalledSinkGetsTheLineAndSeverityAndStderrGetsNothing) {
  SetSink(&CapturingSink);
  testing::internal::CaptureStderr();
  Write(Severity::kInfo, "link recovered (%u frames dropped)", 7u);
  Write(Severity::kError, "short write: %s", "No space left on device");
  EXPECT_EQ(testing::internal::GetCapturedStderr(), "");

  ASSERT_EQ(Captured().size(), 2u);
  EXPECT_EQ(Captured()[0].severity, Severity::kInfo);
  EXPECT_EQ(Captured()[0].text, "link recovered (7 frames dropped)");
  EXPECT_TRUE(Captured()[0].nul_terminated);
  EXPECT_EQ(Captured()[1].severity, Severity::kError);
  EXPECT_EQ(Captured()[1].text, "short write: No space left on device");
}

// The rate-limiting key: one site, one format, whatever its arguments.
TEST_F(LogTest, TheSinkGetsTheFormatTheLineWasBuiltFrom) {
  SetSink(&CapturingSink);
  static constexpr const char* kFormat = "COBS decode failed (%zu bytes)";
  Write(Severity::kWarning, kFormat, std::size_t{3});
  Write(Severity::kWarning, kFormat, std::size_t{9});
  ASSERT_EQ(Captured().size(), 2u);
  EXPECT_EQ(Captured()[0].format, kFormat);
  EXPECT_EQ(Captured()[1].format, kFormat);
  EXPECT_EQ(Captured()[1].text, "COBS decode failed (9 bytes)");
}

TEST_F(LogTest, ClearingTheSinkRestoresStderr) {
  SetSink(&CapturingSink);
  SetSink(nullptr);
  testing::internal::CaptureStderr();
  Write(Severity::kWarning, "back on stderr");
  EXPECT_EQ(testing::internal::GetCapturedStderr(), "back on stderr\n");
  EXPECT_TRUE(Captured().empty());
}

// A sink appends its own line ending; one in the text would print a blank line
// after every message.
TEST_F(LogTest, TrailingLineEndingsAreStripped) {
  SetSink(&CapturingSink);
  Write(Severity::kWarning, "cannot open %s\r\n\n", "part.mcap");
  ASSERT_EQ(Captured().size(), 1u);
  EXPECT_EQ(Captured()[0].text, "cannot open part.mcap");
}

// Cut, not dropped, and visibly cut: a reader must not take a truncated path
// for the whole one.
TEST_F(LogTest, ALongLineIsCutAtTheLimitWithAMarker) {
  SetSink(&CapturingSink);
  const std::string path(kMaxLineBytes * 2, 'p');
  Write(Severity::kWarning, "cannot open %s", path.c_str());
  ASSERT_EQ(Captured().size(), 1u);
  const std::string& text = Captured()[0].text;
  EXPECT_EQ(text.size(), kMaxLineBytes);
  EXPECT_EQ(text.rfind("cannot open ppp", 0), 0u);
  EXPECT_EQ(text.substr(text.size() - kCutMarker.size()), kCutMarker);
  EXPECT_TRUE(Captured()[0].nul_terminated);
}

// The same edge on the default path, where the newline goes into the byte
// past the limit.
TEST_F(LogTest, ALongLineOnStderrIsCutAndStillNewlineTerminated) {
  const std::string path(kMaxLineBytes * 2, 'p');
  testing::internal::CaptureStderr();
  Write(Severity::kWarning, "cannot open %s", path.c_str());
  const std::string out = testing::internal::GetCapturedStderr();
  ASSERT_EQ(out.size(), kMaxLineBytes + 1);
  EXPECT_EQ(out.back(), '\n');
  EXPECT_EQ(out.substr(0, 11), "cannot open");
}

// A wide character with no multibyte form makes vsnprintf fail under any
// locale; nothing half-formatted reaches the sink.
TEST_F(LogTest, ALineThatCannotBeFormattedIsNotDelivered) {
  SetSink(&CapturingSink);
  const wchar_t invalid[] = {static_cast<wchar_t>(0x110000), 0};
  Write(Severity::kWarning, "bad %ls", invalid);
  EXPECT_TRUE(Captured().empty());
}

}  // namespace
