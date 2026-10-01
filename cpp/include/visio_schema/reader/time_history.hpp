// Orientation at an arbitrary instant: a per-stream quaternion history.
//
// The question this answers is "what was this IMU's orientation when THAT frame
// was exposed", where the frame's result may arrive hundreds of milliseconds
// after the IMU samples around it. It is the C++ twin of the Python reader's
// orientation blend (`visio_schema.reader.interp`, `Orientation`), and both are
// pinned to one kernel by tests/golden/slerp_vectors.txt.
//
// Three rules, each of which the obvious simpler version gets wrong:
//
//  * NEVER EXTRAPOLATE. Past either end of the history the answer is the end
//    sample, and only if it is within `max_gap_ns`; otherwise there is no
//    answer. A residual built against a guessed IMU pose is a guess the
//    downstream filter would treat as a measurement.
//
//  * INSERT LATE SAMPLES, DO NOT RESET. A stream relayed through a hub, or
//    reordered by a transport, can deliver a sample older than the newest one
//    already held. Treating that as a new epoch (the behaviour this replaces
//    in fsglove's Python history) throws away exactly the samples a late
//    lookup needs. Only a rewind larger than `rewind_reset_ns` — a clock step,
//    a replay seek — is an epoch change.
//
//  * BOUND BY AGE, NOT COUNT. Streams run at 60 Hz or 470 Hz, and what a
//    consumer needs is "the last N seconds", which a fixed count would express
//    differently per stream.
//
// Quaternions are (x, y, z, w), the wire order. Timestamps are the bus HEADER
// stamps: the only ones on a common clock across devices. Not thread-safe: one
// owner thread adds and looks up.
#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace visio_schema::reader {

using QuatXyzw = std::array<double, 4>;

// Spherical linear interpolation of unit quaternions, (x, y, z, w).
//
// The same formula as `reader.interp.slerp_xyzw`, branch for branch: the short
// arc (q1 is negated when the dot product is negative, since q and -q are one
// rotation), a normalised lerp once the two are within ~1.8 deg (dot > 0.9995,
// where sin(theta) is too small to divide by), and a final renormalisation.
// `w` is not clamped: w > 1 extrapolates, and whether a caller may do that is
// the caller's decision. Non-unit input throws: an all-zero "no pose" sentinel
// blended against a real rotation would come out as a plausible rotation.
inline QuatXyzw SlerpXyzw(const QuatXyzw& q0, const QuatXyzw& in1, double w) {
  auto norm = [](const QuatXyzw& q) {
    return std::sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
  };
  for (const auto* q : {&q0, &in1}) {
    const double n = norm(*q);
    if (!(std::fabs(n - 1.0) <= 1e-3)) {
      throw std::invalid_argument("SlerpXyzw: quaternion norm " + std::to_string(n) +
                                  " is not 1 -- that is not a rotation");
    }
  }
  QuatXyzw q1 = in1;
  double dot = q0[0] * q1[0] + q0[1] * q1[1] + q0[2] * q1[2] + q0[3] * q1[3];
  if (dot < 0.0) {
    for (double& c : q1) c = -c;
    dot = -dot;
  }
  dot = std::min(dot, 1.0);
  QuatXyzw out;
  if (dot > 0.9995) {
    for (int i = 0; i < 4; ++i) out[i] = q0[i] + w * (q1[i] - q0[i]);
  } else {
    const double theta = std::acos(dot);
    const double s = std::sin(theta);
    const double a = std::sin((1.0 - w) * theta) / s;
    const double b = std::sin(w * theta) / s;
    for (int i = 0; i < 4; ++i) out[i] = a * q0[i] + b * q1[i];
  }
  const double n = norm(out);
  for (double& c : out) c /= n;
  return out;
}

// How a lookup's answer was produced. A consumer that fuses on it should know
// whether it got a blend of two real samples or the nearest end held over.
enum class SampleMethod { kInterpolated, kHeld };

struct QuatSample {
  QuatXyzw q{};
  // Distance to the nearer real sample. 0 on an exact hit.
  std::int64_t gap_ns = 0;
  SampleMethod method = SampleMethod::kInterpolated;
};

class QuatHistory {
 public:
  static constexpr std::int64_t kDefaultMaxAgeNs = 3'000'000'000;
  static constexpr std::int64_t kDefaultRewindResetNs = 1'000'000'000;

  explicit QuatHistory(std::int64_t max_age_ns = kDefaultMaxAgeNs,
                       std::int64_t rewind_reset_ns = kDefaultRewindResetNs)
      : max_age_ns_(max_age_ns), rewind_reset_ns_(rewind_reset_ns) {}

  // Returns false (and stores nothing) for a quaternion that is not a rotation:
  // non-finite, or a norm more than 1e-3 away from 1. Such input is renormalised
  // by nothing downstream, so it must not enter the history.
  bool Add(std::int64_t t_ns, const QuatXyzw& q) {
    const double n = std::sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
    if (!std::isfinite(n) || !(std::fabs(n - 1.0) <= 1e-3)) return false;
    const QuatXyzw u{q[0] / n, q[1] / n, q[2] / n, q[3] / n};
    if (!s_.empty() && t_ns < s_.back().t - rewind_reset_ns_) {
      s_.clear();  // a clock step or replay seek: a new epoch, not a late sample
    }
    // Upper bound, so the common in-order append is O(1) amortised.
    auto it = std::upper_bound(s_.begin(), s_.end(), t_ns,
                               [](std::int64_t t, const Entry& e) { return t < e.t; });
    if (it != s_.begin() && std::prev(it)->t == t_ns) {
      std::prev(it)->q = u;  // a repeated stamp replaces: a zero-width bracket has no blend
    } else {
      s_.insert(it, Entry{t_ns, u});
    }
    const std::int64_t horizon = s_.back().t - max_age_ns_;
    auto keep = std::lower_bound(s_.begin(), s_.end(), horizon,
                                 [](const Entry& e, std::int64_t t) { return e.t < t; });
    s_.erase(s_.begin(), keep);
    return true;
  }

  // The orientation at `t_ns`, or nullopt if no real sample lies within
  // `max_gap_ns` of it. Inside the history the two bracketing samples are
  // SLERPed; if the nearer of them is further than `max_gap_ns`, the gap is
  // too wide to trust a blend across and there is no answer. At either end the
  // end sample is held, never extrapolated.
  std::optional<QuatSample> At(std::int64_t t_ns, std::int64_t max_gap_ns) const {
    if (s_.empty()) return std::nullopt;
    if (t_ns <= s_.front().t) return Held(s_.front(), s_.front().t - t_ns, max_gap_ns);
    if (t_ns >= s_.back().t) return Held(s_.back(), t_ns - s_.back().t, max_gap_ns);
    auto hi = std::lower_bound(s_.begin(), s_.end(), t_ns,
                               [](const Entry& e, std::int64_t t) { return e.t < t; });
    if (hi->t == t_ns) return QuatSample{hi->q, 0, SampleMethod::kInterpolated};
    const Entry& lo = *std::prev(hi);
    const std::int64_t gap = std::min(t_ns - lo.t, hi->t - t_ns);
    if (gap > max_gap_ns) return std::nullopt;
    const double w = static_cast<double>(t_ns - lo.t) / static_cast<double>(hi->t - lo.t);
    return QuatSample{SlerpXyzw(lo.q, hi->q, w), gap, SampleMethod::kInterpolated};
  }

  bool empty() const { return s_.empty(); }
  std::size_t size() const { return s_.size(); }
  std::int64_t oldest_ns() const { return s_.empty() ? 0 : s_.front().t; }
  std::int64_t newest_ns() const { return s_.empty() ? 0 : s_.back().t; }
  void Clear() { s_.clear(); }

 private:
  struct Entry {
    std::int64_t t;
    QuatXyzw q;
  };

  static std::optional<QuatSample> Held(const Entry& e, std::int64_t gap,
                                        std::int64_t max_gap_ns) {
    if (gap > max_gap_ns) return std::nullopt;
    return QuatSample{e.q, gap, gap == 0 ? SampleMethod::kInterpolated : SampleMethod::kHeld};
  }

  std::int64_t max_age_ns_;
  std::int64_t rewind_reset_ns_;
  std::vector<Entry> s_;  // sorted by t, unique t
};

// One history per slot (e.g. per glove IMU). Slots appear on first Add.
class SlotQuatHistory {
 public:
  explicit SlotQuatHistory(std::int64_t max_age_ns = QuatHistory::kDefaultMaxAgeNs,
                           std::int64_t rewind_reset_ns = QuatHistory::kDefaultRewindResetNs)
      : max_age_ns_(max_age_ns), rewind_reset_ns_(rewind_reset_ns) {}

  bool Add(int slot, std::int64_t t_ns, const QuatXyzw& q) {
    auto it = by_slot_.find(slot);
    if (it == by_slot_.end()) {
      it = by_slot_.emplace(slot, QuatHistory(max_age_ns_, rewind_reset_ns_)).first;
    }
    return it->second.Add(t_ns, q);
  }

  std::optional<QuatSample> At(int slot, std::int64_t t_ns, std::int64_t max_gap_ns) const {
    const auto it = by_slot_.find(slot);
    if (it == by_slot_.end()) return std::nullopt;
    return it->second.At(t_ns, max_gap_ns);
  }

  std::vector<int> Slots() const {
    std::vector<int> out;
    out.reserve(by_slot_.size());
    for (const auto& kv : by_slot_) out.push_back(kv.first);
    return out;
  }

  void Clear() { by_slot_.clear(); }

 private:
  std::int64_t max_age_ns_;
  std::int64_t rewind_reset_ns_;
  std::map<int, QuatHistory> by_slot_;
};

}  // namespace visio_schema::reader
