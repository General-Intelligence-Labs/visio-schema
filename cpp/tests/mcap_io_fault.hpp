// Faults apply only to this test's temporary recording root. Separate binary:
// no stdio interposer is installed beneath the general schema test suite.
#pragma once

#include <atomic>
#include <string>

namespace mcap_io_test {

enum class Fault { None, ShortWrite, WriteError, Flush, Close, CloseMeasure, FileSync, DirSync };

class FaultScope {
 public:
  FaultScope(std::string root, Fault fault);
  ~FaultScope();
  FaultScope(const FaultScope&) = delete;
  FaultScope& operator=(const FaultScope&) = delete;
  unsigned hits() const;
};

bool inject(int fd, Fault fault);

}  // namespace mcap_io_test
