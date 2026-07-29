// StreamRule / ResolvedStreamPolicy — what ONE connection wants delivered.
//
// The wire form (SetStreamPolicy, command.proto) names streams by topic glob;
// whoever owns the registry resolves those globs into this id-keyed table. The
// endpoint therefore enforces without knowing about topics or commands.
//
// Lives under transport/, not routing/, so the dependency runs one way: the
// enforcing layer owns the shape it enforces, and the resolver (which needs both
// this and routing/topic_match.hpp) sits above the two.
#pragma once

#include <cstdint>
#include <unordered_map>

namespace visio_schema::transport {

struct StreamRule {
  // Send nothing at all on this stream. Wins over min_gap_us.
  bool drop = false;

  // Minimum spacing between delivered messages, microseconds. 0 = full rate.
  //
  // Pre-divided from the wire's max_rate_hz by the resolver: this is read on
  // every message of a capped stream, and armv7 without idiv turns a division
  // there into an `__aeabi_uidiv` call.
  //
  // Ignored for camera video: H.265 is inter-coded, so shedding arbitrary
  // P-frames costs the decoder its reference chain and blanks the viewer for a
  // whole GOP. Video is keep-or-drop; capping is for streams whose messages
  // stand alone (fused IMU quaternions, raw IMU bundles).
  std::int64_t min_gap_us = 0;
};

// stream_id -> rule. ONLY streams a rule actually restricts appear: an absent id
// means "deliver at full rate", so a newly announced stream is delivered until a
// rule is resolved for it rather than silently dropped.
using ResolvedStreamPolicy = std::unordered_map<std::uint32_t, StreamRule>;

}  // namespace visio_schema::transport
