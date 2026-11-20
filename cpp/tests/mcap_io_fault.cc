#include "mcap_io_fault.hpp"

#include <dlfcn.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <stdexcept>
#include <utility>
#include <cstring>

#define MCAP_COMPRESSION_NO_LZ4
#define MCAP_COMPRESSION_NO_ZSTD
#include <mcap/writer.hpp>

extern "C" void* __real__Znwm(std::size_t);
extern "C" void __real__ZdlPv(void*) noexcept;
extern "C" void __real__ZdlPvm(void*, std::size_t) noexcept;

namespace mcap_io_test {
namespace {
std::string recording_root;
std::atomic<Fault> active{Fault::None};
std::atomic<unsigned> fault_hits{0};
thread_local CloseOrderScope* close_order = nullptr;

bool InRoot(int fd, const std::string& root) {
  char link[64], path[4096];
  std::snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
  const auto n = ::readlink(link, path, sizeof(path));
  if (n < 0 || static_cast<std::size_t>(n) < root.size()) return false;
  return std::memcmp(path, root.data(), root.size()) == 0 &&
      (static_cast<std::size_t>(n) == root.size() || path[root.size()] == '/');
}
}

FaultScope::FaultScope(std::string root, Fault fault) {
  if (active.load() != Fault::None) throw std::logic_error("nested I/O fault");
  recording_root = std::move(root);
  fault_hits.store(0);
  active.store(fault, std::memory_order_release);
}

FaultScope::~FaultScope() { active.store(Fault::None); }
unsigned FaultScope::hits() const { return fault_hits.load(); }

bool inject(int fd, Fault fault) {
  if (active.load(std::memory_order_acquire) != fault) return false;
  if (!InRoot(fd, recording_root)) return false;
  struct stat64 st{};
  using Stat = int (*)(int, struct stat64*);
  static const Stat real_stat = reinterpret_cast<Stat>(::dlsym(RTLD_NEXT, "fstat64"));
  if (real_stat(fd, &st) != 0) return false;
  if ((fault == Fault::FileSync || fault == Fault::FileSyncThrow) &&
      !S_ISREG(st.st_mode)) return false;
  if (fault == Fault::DirSync && !S_ISDIR(st.st_mode)) return false;
  ++fault_hits;
  if (fault == Fault::FileSyncThrow) active.store(Fault::None);
  errno = fault == Fault::ShortWrite ? ENOSPC : EIO;
  return true;
}
CloseOrderScope::CloseOrderScope(const std::string& root) : root_(root) {
  if (close_order) throw std::logic_error("nested close observation");
  close_order = this;
}
CloseOrderScope::~CloseOrderScope() { close_order = nullptr; }

void CloseOrderScope::Allocated(void* address, std::size_t size) {
  if (size != sizeof(::mcap::McapWriter)) return;
  ++stats_.acquired;
  if (writer_) ++stats_.overlapping;
  writer_ = reinterpret_cast<std::uintptr_t>(address);
}
void CloseOrderScope::Released(void* address) {
  if (!writer_ || reinterpret_cast<std::uintptr_t>(address) != writer_) return;
  ++stats_.released;
  writer_ = 0;
}
void CloseOrderScope::Barrier(int fd, bool close) {
  if (!InRoot(fd, root_)) return;
  struct stat64 st{};
  if (::fstat64(fd, &st) != 0 || !S_ISREG(st.st_mode)) return;
  if (close) ++stats_.closes;
  else ++stats_.checkpoints;
  if (!stats_.acquired || writer_) ++stats_.early_barriers;
}

void ObserveAllocation(void* address, std::size_t size) {
  if (close_order) close_order->Allocated(address, size);
}
void ObserveRelease(void* address) {
  if (close_order) close_order->Released(address);
}
void ObserveBarrier(int fd, bool close) {
  if (close_order) close_order->Barrier(fd, close);
}
}  // namespace mcap_io_test

extern "C" void* __wrap__Znwm(std::size_t size) {
  auto* address = __real__Znwm(size);
  mcap_io_test::ObserveAllocation(address, size);
  return address;
}
extern "C" void __wrap__ZdlPv(void* address) noexcept {
  mcap_io_test::ObserveRelease(address);
  __real__ZdlPv(address);
}
extern "C" void __wrap__ZdlPvm(void* address, std::size_t size) noexcept {
  mcap_io_test::ObserveRelease(address);
  __real__ZdlPvm(address, size);
}

extern "C" size_t fwrite(const void* data, size_t size, size_t count, FILE* file) {
  using Fn = size_t (*)(const void*, size_t, size_t, FILE*);
  static const Fn real = reinterpret_cast<Fn>(::dlsym(RTLD_NEXT, "fwrite"));
  if (size && count && mcap_io_test::inject(::fileno(file), mcap_io_test::Fault::WriteError))
    return 0;
  if (size && count && mcap_io_test::inject(::fileno(file), mcap_io_test::Fault::ShortWrite)) {
    const size_t written = real(data, size, count / 2, file);
    errno = ENOSPC;
    return written;
  }
  return real(data, size, count, file);
}

extern "C" int fflush(FILE* file) {
  using Fn = int (*)(FILE*);
  static const Fn real = reinterpret_cast<Fn>(::dlsym(RTLD_NEXT, "fflush"));
  if (file && mcap_io_test::inject(::fileno(file), mcap_io_test::Fault::Flush)) return EOF;
  return real(file);
}

extern "C" int fclose(FILE* file) {
  mcap_io_test::ObserveBarrier(::fileno(file), true);
  using Fn = int (*)(FILE*);
  static const Fn real = reinterpret_cast<Fn>(::dlsym(RTLD_NEXT, "fclose"));
  const bool fail = mcap_io_test::inject(::fileno(file), mcap_io_test::Fault::Close);
  const int result = real(file);  // close even on error, as Linux does
  if (fail) { errno = EIO; return EOF; }
  return result;
}

extern "C" int fsync(int fd) {
  mcap_io_test::ObserveBarrier(fd, false);
  using Fn = int (*)(int);
  static const Fn real = reinterpret_cast<Fn>(::dlsym(RTLD_NEXT, "fsync"));
  if (mcap_io_test::inject(fd, mcap_io_test::Fault::FileSyncThrow))
    throw std::runtime_error("injected final checkpoint exception");
  if (mcap_io_test::inject(fd, mcap_io_test::Fault::FileSync) ||
      mcap_io_test::inject(fd, mcap_io_test::Fault::DirSync)) return -1;
  return real(fd);
}

extern "C" int fstat64(int fd, struct stat64* st) noexcept {
  using Fn = int (*)(int, struct stat64*);
  static const Fn real = reinterpret_cast<Fn>(::dlsym(RTLD_NEXT, "fstat64"));
  if (mcap_io_test::inject(fd, mcap_io_test::Fault::CloseMeasure)) return -1;
  return real(fd, st);
}
