#include "mcap_io_fault.hpp"

#include <dlfcn.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <stdexcept>
#include <utility>

namespace mcap_io_test {
namespace {
std::string recording_root;
std::atomic<Fault> active{Fault::None};
std::atomic<unsigned> fault_hits{0};
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
  char path[4096];
  const std::string link = "/proc/self/fd/" + std::to_string(fd);
  const ssize_t n = ::readlink(link.c_str(), path, sizeof(path));
  if (n < 0) return false;
  const std::string target(path, static_cast<std::size_t>(n));
  if (target != recording_root &&
      target.compare(0, recording_root.size() + 1, recording_root + "/") != 0)
    return false;
  struct stat64 st{};
  using Stat = int (*)(int, struct stat64*);
  static const Stat real_stat = reinterpret_cast<Stat>(::dlsym(RTLD_NEXT, "fstat64"));
  if (real_stat(fd, &st) != 0) return false;
  if (fault == Fault::FileSync && !S_ISREG(st.st_mode)) return false;
  if (fault == Fault::DirSync && !S_ISDIR(st.st_mode)) return false;
  ++fault_hits;
  errno = fault == Fault::ShortWrite ? ENOSPC : EIO;
  return true;
}
}  // namespace mcap_io_test

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
  using Fn = int (*)(FILE*);
  static const Fn real = reinterpret_cast<Fn>(::dlsym(RTLD_NEXT, "fclose"));
  const bool fail = mcap_io_test::inject(::fileno(file), mcap_io_test::Fault::Close);
  const int result = real(file);  // close even on error, as Linux does
  if (fail) { errno = EIO; return EOF; }
  return result;
}

extern "C" int fsync(int fd) {
  using Fn = int (*)(int);
  static const Fn real = reinterpret_cast<Fn>(::dlsym(RTLD_NEXT, "fsync"));
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
