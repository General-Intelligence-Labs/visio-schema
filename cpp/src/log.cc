#include "visio_schema/log.hpp"

#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstring>

namespace visio_schema::log {
namespace {

std::atomic<Sink> g_sink{nullptr};

// `line` has room for one byte past `len`. The line and its newline go out in
// ONE write, so lines from concurrent threads never splice into each other.
void WriteToStderr(char* line, std::size_t len) {
  line[len++] = '\n';
  while (len > 0) {
    const ssize_t n = ::write(STDERR_FILENO, line, len);
    if (n <= 0) {
      if (n < 0 && errno == EINTR) continue;
      return;  // nowhere left to report it
    }
    line += n;
    len -= static_cast<std::size_t>(n);
  }
}

}  // namespace

void SetSink(Sink sink) { g_sink.store(sink, std::memory_order_release); }

void Write(Severity severity, const char* format, ...) {
  char line[kMaxLineBytes + 1];
  va_list ap;
  va_start(ap, format);
  const int n = std::vsnprintf(line, sizeof(line), format, ap);
  va_end(ap);
  if (n < 0) return;
  std::size_t len = static_cast<std::size_t>(n);
  if (len > kMaxLineBytes) {
    len = kMaxLineBytes;
    std::memcpy(line + len - kCutMarker.size(), kCutMarker.data(),
                kCutMarker.size());
  }
  while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r')) --len;
  if (const Sink sink = g_sink.load(std::memory_order_acquire)) {
    line[len] = '\0';
    sink(Record{severity, format, std::string_view(line, len)});
    return;
  }
  WriteToStderr(line, len);
}

}  // namespace visio_schema::log
