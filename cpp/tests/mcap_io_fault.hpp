// Faults apply only to this test's temporary recording root. Separate binary:
// no stdio interposer is installed beneath the general schema test suite.
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>

namespace mcap_io_test {

enum class Fault {
  None, ShortWrite, WriteError, Flush, Close, CloseMeasure, FileSync, DirSync,
  FileSyncThrow
};

class FaultScope {
 public:
  FaultScope(std::string root, Fault fault);
  ~FaultScope();
  FaultScope(const FaultScope&) = delete;
  FaultScope& operator=(const FaultScope&) = delete;
  unsigned hits() const;
};

// Test-only observation of the real upstream writer and final file barriers.
// Linker wrappers delegate to the normal allocator; no RSS estimate is used.
struct CloseOrderStats {
  unsigned acquired = 0, released = 0;
  unsigned checkpoints = 0, closes = 0;
  unsigned early_barriers = 0, overlapping = 0;
};

class CloseOrderScope {
 public:
  explicit CloseOrderScope(const std::string& root);
  ~CloseOrderScope();
  CloseOrderScope(const CloseOrderScope&) = delete;
  CloseOrderScope& operator=(const CloseOrderScope&) = delete;
  CloseOrderStats stats() const { return stats_; }
  void Allocated(void* address, std::size_t size);
  void Released(void* address);
  void Barrier(int fd, bool close);

 private:
  const std::string& root_;
  std::uintptr_t writer_ = 0;
  CloseOrderStats stats_;
};

bool inject(int fd, Fault fault);

}  // namespace mcap_io_test
