// log.hpp — where the C++ library reports what it notices while running: a
// link that stalls or recovers, a write that fails, a read-back mismatch.
//
// By default each line is one write(2) to stderr. An application that keeps
// its own log installs a sink once, before starting any endpoint or writer,
// and every line goes there instead, with its severity. A bus built on this
// library reports through the same sink, so one installation covers both.
#pragma once

#include <cstddef>
#include <string_view>

namespace visio_schema::log {

enum class Severity : char {
  kInfo = 'I',     // a state change that needs no action, or a result
  kWarning = 'W',  // degraded but continuing: data may be dropped or late
  kError = 'E',    // an operation failed
};

// The longest line delivered. A longer one is cut to this many bytes, the
// last of them replaced by kCutMarker.
inline constexpr std::size_t kMaxLineBytes = 1023;
inline constexpr std::string_view kCutMarker = " ...";

struct Record {
  Severity severity;
  // The printf format the line was built from: the reporting site's string
  // literal, so its ADDRESS identifies the site. A rate-limiting sink keys on
  // it, and that is why every site passes a literal of its own — two sites
  // sharing one would share a budget, and a frequent one could silence a rare
  // one.
  const char* format;
  // The line, without a line ending. text.data()[text.size()] is '\0'.
  std::string_view text;
};

// Called on whichever thread reported, so it must be thread-safe, and it must
// not block for long: the reporting thread may be an I/O loop.
using Sink = void (*)(const Record& record);

// Send every line to `sink`; nullptr restores the default. The swap is
// atomic, but a line already on its way to the previous sink finishes there.
void SetSink(Sink sink);

// printf-style. Line endings at the end are dropped; a line that cannot be
// formatted is not delivered.
void Write(Severity severity, const char* format, ...)
#if defined(__GNUC__) || defined(__clang__)
    __attribute__((format(printf, 2, 3)))
#endif
    ;

}  // namespace visio_schema::log
