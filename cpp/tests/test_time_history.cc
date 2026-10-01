// reader/time_history.hpp: the SLERP kernel against the cross-language golden
// file, and the history's lookup rules (hold, never extrapolate, insert late
// samples, reset only on a real rewind, bound by age).
#include "visio_schema/reader/time_history.hpp"

#include <gtest/gtest.h>

#include <cmath>
#include <cstring>

#include "golden_vectors_test_util.hpp"

using visio_schema::reader::QuatHistory;
using visio_schema::reader::QuatXyzw;
using visio_schema::reader::SampleMethod;
using visio_schema::reader::SlerpXyzw;
using visio_schema::reader::SlotQuatHistory;

namespace {

constexpr std::int64_t kMs = 1'000'000;

QuatXyzw RotZ(double deg) {
  const double h = deg * M_PI / 360.0;
  return {0.0, 0.0, std::sin(h), std::cos(h)};
}

// Rotation angle about +z of a pure-z quaternion, degrees.
double YawDeg(const QuatXyzw& q) { return 2.0 * std::atan2(q[2], q[3]) * 180.0 / M_PI; }

double LeDouble(const std::string& bytes, int index) {
  double v;
  std::memcpy(&v, bytes.data() + index * 8, 8);  // the file is little-endian, as is every target
  return v;
}

}  // namespace

TEST(SlerpKernel, ReproducesThePythonReferenceOnEveryGoldenCase) {
  const auto cases = visio_golden::Load("slerp_vectors.txt");
  ASSERT_GE(cases.size(), 30u) << "golden file missing or truncated";
  for (const auto& [name, bytes] : cases) {
    ASSERT_EQ(bytes.size(), 13u * 8u) << name;
    QuatXyzw q0, q1, want;
    for (int i = 0; i < 4; ++i) {
      q0[i] = LeDouble(bytes, i);
      q1[i] = LeDouble(bytes, 4 + i);
      want[i] = LeDouble(bytes, 9 + i);
    }
    const QuatXyzw got = SlerpXyzw(q0, q1, LeDouble(bytes, 8));
    for (int i = 0; i < 4; ++i) {
      // Not bitwise: numpy and libm may round sin/acos differently in the last
      // ulp. 1e-12 is ~1e-10 degrees, far below anything a consumer can see.
      EXPECT_NEAR(got[i], want[i], 1e-12) << name << " component " << i;
    }
  }
}

TEST(SlerpKernel, TakesTheShortArcAcrossTheSignBoundary) {
  const QuatXyzw a = RotZ(10);
  QuatXyzw b = RotZ(50);
  for (double& c : b) c = -c;  // the same rotation, written the long way round
  EXPECT_NEAR(YawDeg(SlerpXyzw(a, b, 0.5)), 30.0, 1e-9);
}

TEST(SlerpKernel, RefusesSomethingThatIsNotARotation) {
  EXPECT_THROW(SlerpXyzw({0, 0, 0, 0}, RotZ(10), 0.5), std::invalid_argument);
}

TEST(QuatHistory, InterpolatesBetweenTheBracketingSamples) {
  QuatHistory h;
  ASSERT_TRUE(h.Add(100 * kMs, RotZ(0)));
  ASSERT_TRUE(h.Add(116 * kMs, RotZ(16)));
  const auto s = h.At(104 * kMs, 25 * kMs);
  ASSERT_TRUE(s.has_value());
  EXPECT_EQ(s->method, SampleMethod::kInterpolated);
  EXPECT_EQ(s->gap_ns, 4 * kMs);
  EXPECT_NEAR(YawDeg(s->q), 4.0, 1e-9);
}

TEST(QuatHistory, HoldsTheEndsWithinTheGapAndNeverExtrapolates) {
  QuatHistory h;
  h.Add(100 * kMs, RotZ(0));
  h.Add(116 * kMs, RotZ(16));
  // Past the newest sample: the newest is held, not projected forward.
  const auto after = h.At(126 * kMs, 25 * kMs);
  ASSERT_TRUE(after.has_value());
  EXPECT_EQ(after->method, SampleMethod::kHeld);
  EXPECT_NEAR(YawDeg(after->q), 16.0, 1e-9);
  EXPECT_EQ(after->gap_ns, 10 * kMs);
  // Before the oldest: likewise.
  const auto before = h.At(90 * kMs, 25 * kMs);
  ASSERT_TRUE(before.has_value());
  EXPECT_NEAR(YawDeg(before->q), 0.0, 1e-9);
  // Beyond the gap at either end: no answer.
  EXPECT_FALSE(h.At(142 * kMs, 25 * kMs).has_value());
  EXPECT_FALSE(h.At(74 * kMs, 25 * kMs).has_value());
}

TEST(QuatHistory, RefusesToBlendAcrossAHoleWiderThanTheGap) {
  QuatHistory h;
  h.Add(0, RotZ(0));
  h.Add(200 * kMs, RotZ(90));  // a 200 ms dropout
  EXPECT_FALSE(h.At(100 * kMs, 25 * kMs).has_value());
  EXPECT_TRUE(h.At(20 * kMs, 25 * kMs).has_value());  // near an end of the hole is fine
}

TEST(QuatHistory, InsertsALateSampleInsteadOfStartingOver) {
  QuatHistory h;
  h.Add(100 * kMs, RotZ(0));
  h.Add(132 * kMs, RotZ(32));
  h.Add(116 * kMs, RotZ(16));  // arrives last, belongs in the middle
  EXPECT_EQ(h.size(), 3u);
  EXPECT_EQ(h.oldest_ns(), 100 * kMs);
  const auto s = h.At(124 * kMs, 25 * kMs);
  ASSERT_TRUE(s.has_value());
  EXPECT_NEAR(YawDeg(s->q), 24.0, 1e-9);
}

TEST(QuatHistory, ARealRewindStartsANewEpoch) {
  QuatHistory h;  // rewind_reset = 1 s
  h.Add(5'000 * kMs, RotZ(50));
  h.Add(5'016 * kMs, RotZ(51));
  h.Add(1'000 * kMs, RotZ(10));  // 4 s back: a clock step or a replay seek
  EXPECT_EQ(h.size(), 1u);
  EXPECT_EQ(h.newest_ns(), 1'000 * kMs);
}

TEST(QuatHistory, ARepeatedStampReplacesRatherThanMakingAZeroWidthBracket) {
  QuatHistory h;
  h.Add(100 * kMs, RotZ(0));
  h.Add(100 * kMs, RotZ(5));
  EXPECT_EQ(h.size(), 1u);
  EXPECT_NEAR(YawDeg(h.At(100 * kMs, 0)->q), 5.0, 1e-9);
}

TEST(QuatHistory, IsBoundedByAgeRelativeToTheNewestSample) {
  QuatHistory h(/*max_age_ns=*/1'000 * kMs);
  for (int i = 0; i <= 200; ++i) h.Add(i * 10 * kMs, RotZ(0));  // 2 s at 100 Hz
  EXPECT_EQ(h.newest_ns(), 2'000 * kMs);
  EXPECT_EQ(h.oldest_ns(), 1'000 * kMs);
}

TEST(QuatHistory, RejectsANonRotationWithoutStoringIt) {
  QuatHistory h;
  EXPECT_FALSE(h.Add(0, {0, 0, 0, 0}));  // the "no pose" sentinel
  EXPECT_FALSE(h.Add(0, {NAN, 0, 0, 1}));
  EXPECT_TRUE(h.empty());
}

TEST(SlotQuatHistory, KeepsEachSlotSeparate) {
  SlotQuatHistory h;
  h.Add(3, 100 * kMs, RotZ(10));
  h.Add(7, 100 * kMs, RotZ(70));
  EXPECT_NEAR(YawDeg(h.At(3, 100 * kMs, 0)->q), 10.0, 1e-9);
  EXPECT_NEAR(YawDeg(h.At(7, 100 * kMs, 0)->q), 70.0, 1e-9);
  EXPECT_FALSE(h.At(9, 100 * kMs, 25 * kMs).has_value());
  EXPECT_EQ(h.Slots(), (std::vector<int>{3, 7}));
}
