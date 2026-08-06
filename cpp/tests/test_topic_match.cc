// The stream-policy glob grammar (routing/topic_match.hpp).
//
// This is the language a preview writes its bandwidth rules in, and a rule that
// matches one topic too many silently deletes a stream someone needed — so the
// edges (segment counts, the '*' vs '**' distinction, leading slashes) are
// pinned here rather than left to the matcher's implementation.
#include <gtest/gtest.h>

#include "visio_schema/routing/topic_match.hpp"

using visio_schema::routing::TopicMatches;

TEST(TopicMatch, LiteralNeedsEverySegment) {
  EXPECT_TRUE(TopicMatches("/ego/camera/0", "/ego/camera/0"));
  EXPECT_FALSE(TopicMatches("/ego/camera/0", "/ego/camera/1"));
  EXPECT_FALSE(TopicMatches("/ego/camera", "/ego/camera/0"));
  EXPECT_FALSE(TopicMatches("/ego/camera/0/extra", "/ego/camera/0"));
}

// A hub prefixes a relayed leaf topic with the leaf's device_name, and a client
// writing rules ahead of discovery does not know it. Normalising the leading
// slash is what lets "*/camera/0" be written once and match either form.
TEST(TopicMatch, LeadingSlashIsOptionalOnBothSides) {
  EXPECT_TRUE(TopicMatches("ego/camera/0", "/ego/camera/0"));
  EXPECT_TRUE(TopicMatches("/ego/camera/0", "ego/camera/0"));
  EXPECT_TRUE(TopicMatches("*/camera/0", "/ego/camera/0"));
}

TEST(TopicMatch, SingleStarIsExactlyOneSegment) {
  EXPECT_TRUE(TopicMatches("*/camera/*", "/ego/camera/0"));
  EXPECT_TRUE(TopicMatches("*/imu/*/raw", "/ego/imu/0/raw"));
  EXPECT_TRUE(TopicMatches("*/imu/*/raw", "/glove_left/imu/15/raw"));
  // One segment, never several: the whole point of the "drop the other eye"
  // rule is that it must NOT also swallow /ego/camera/0/annotations.
  EXPECT_FALSE(TopicMatches("*/camera/*", "/ego/camera/0/annotations"));
  EXPECT_FALSE(TopicMatches("*/camera/*", "/ego/camera"));
}

// A relay MAY namespace relayed leaf topics under the leaf's device_name
// (device-name prefixing — off by default, opted into for multi-device
// bring-up). One pattern has to cover both depths; a trailing-only '**' could
// express neither.
TEST(TopicMatch, DoubleStarMatchesEitherTopicDepth) {
  EXPECT_TRUE(TopicMatches("**/camera/0", "/GILABS-AABBCCDD/ego/camera/0"));
  EXPECT_TRUE(TopicMatches("**/camera/0", "/ego/camera/0"));
  EXPECT_TRUE(TopicMatches("**/imu/*/raw", "/GILABS-AABBCCDD/ego/imu/0/raw"));
  EXPECT_TRUE(TopicMatches("**/imu/*/raw", "/ego/imu/0/raw"));
  // Still exact about the tail, at any prefix depth.
  EXPECT_FALSE(TopicMatches("**/camera/0", "/GILABS-AABBCCDD/ego/camera/1"));
  EXPECT_FALSE(TopicMatches("**/camera/0", "/ego/camera/0/annotations"));
}

// '**' in the middle, and the backtracking a greedy scan needs to get it right.
// The app's drop rule. Deeper channels under a camera (annotations, intrinsics)
// must survive it, at either depth.
TEST(TopicMatch, LeadingDoubleStarStillStopsAtTheNamedSegment) {
  EXPECT_TRUE(TopicMatches("**/camera/*", "/ego/camera/1"));
  EXPECT_TRUE(TopicMatches("**/camera/*", "/GILABS-X/ego/camera/1"));
  EXPECT_FALSE(TopicMatches("**/camera/*", "/ego/camera/1/annotations"));
  EXPECT_FALSE(TopicMatches("**/camera/*", "/GILABS-X/ego/camera/1/intrinsics"));
}

TEST(TopicMatch, DoubleStarInTheMiddleBacktracks) {
  EXPECT_TRUE(TopicMatches("ego/**/raw", "/ego/imu/0/raw"));
  EXPECT_TRUE(TopicMatches("ego/**/raw", "/ego/raw"));
  EXPECT_TRUE(TopicMatches("**/camera/**", "/GILABS-X/ego/camera/0/annotations"));
  // A literal after '**' that never appears must still fail.
  EXPECT_FALSE(TopicMatches("ego/**/quat", "/ego/imu/0/raw"));
}

TEST(TopicMatch, DoubleStarSwallowsTheRestIncludingNothing) {
  EXPECT_TRUE(TopicMatches("*/camera/**", "/ego/camera/0"));
  EXPECT_TRUE(TopicMatches("*/camera/**", "/ego/camera/0/annotations"));
  EXPECT_TRUE(TopicMatches("**", "/ego/camera/0"));
  // Zero remaining segments still matches.
  EXPECT_TRUE(TopicMatches("ego/camera/**", "/ego/camera"));
  EXPECT_FALSE(TopicMatches("*/camera/**", "/ego/imu/0/raw"));
}

// No partial-segment globbing: rule order is hard enough to reason about
// without prefix matches, and we have no case that wants it.
TEST(TopicMatch, StarInsideASegmentIsALiteral) {
  EXPECT_FALSE(TopicMatches("/ego/cam*", "/ego/camera"));
  EXPECT_TRUE(TopicMatches("/ego/cam*", "/ego/cam*"));
}

// A filter that matched everything on a malformed rule would be the worst
// possible failure: it would silently delete every stream.
TEST(TopicMatch, EmptyMatchesNothing) {
  EXPECT_FALSE(TopicMatches("", "/ego/camera/0"));
  EXPECT_FALSE(TopicMatches("/", "/ego/camera/0"));
  EXPECT_FALSE(TopicMatches("*/camera/0", ""));
}
