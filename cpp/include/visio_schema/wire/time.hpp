// Monotonic clock + Timestamp helpers — the single source for seq/timesync
// timestamps across the bus, the heartbeat service, and the MCAP writer.
#pragma once

#include <chrono>
#include <cstdint>

#include "google/protobuf/timestamp.pb.h"

namespace visio_schema {

inline std::int64_t MonotonicNs() {
  return std::chrono::steady_clock::now().time_since_epoch().count();
}

inline std::int64_t TimestampNs(const google_protobuf_Timestamp& ts) {
  return static_cast<std::int64_t>(ts.seconds) * 1'000'000'000 + ts.nanos;
}

inline void SetTimestampNs(google_protobuf_Timestamp* ts, std::int64_t ns) {
  ts->seconds = ns / 1'000'000'000;
  ts->nanos = static_cast<std::int32_t>(ns % 1'000'000'000);
}

}  // namespace visio_schema
