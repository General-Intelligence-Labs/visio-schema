// file_sync — the writeback / eviction / durability syscalls the recording
// sink (writer.cc) and its write-time read-back (span_readback.cc) share.
// Private to src/mcap: not installed, not part of the public include tree.
#pragma once

#include <fcntl.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <string>

namespace visio_schema::mcap::file_sync {

// The page the writeback spans, the evictions and O_DIRECT are aligned to.
constexpr std::uint64_t kPageBytes = 4096;
inline std::uint64_t RoundDownToPage(std::uint64_t n) {
  return n & ~(kPageBytes - 1);
}
inline std::uint64_t RoundUpToPage(std::uint64_t n) {
  return RoundDownToPage(n + kPageBytes - 1);
}

// uClibc-ng marshals sync_file_range() WRONG on 32-bit ARM: the kernel's
// only ARM entry point is arm_sync_file_range (= sync_file_range2, flags
// in r1 per the EABI's even-register rule for 64-bit args), but the libc
// stub passes the generic order — the kernel reads flags = 0 and the call
// is a successful no-op. Verified by disassembly of the SDK toolchain's
// libc.so.1 (its posix_fadvise64 does the ARM swizzle correctly; this one
// doesn't). Issue the syscall ourselves on ARM; syscall(2) passes longs
// in r0..r5, which is exactly sync_file_range2's layout.
inline long SyncRange(int fd, std::uint64_t off, std::uint64_t len,
                      unsigned int flags) {
#if defined(__linux__) && defined(__arm__)
#ifndef __NR_sync_file_range2
#error "32-bit ARM needs sync_file_range2 (the libc stub is broken)"
#endif
  static_assert(__BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__,
                "the lo/hi register split below assumes little-endian");
  return ::syscall(__NR_sync_file_range2, fd, flags,
                   static_cast<unsigned long>(off & 0xffffffffu),
                   static_cast<unsigned long>(off >> 32),
                   static_cast<unsigned long>(len & 0xffffffffu),
                   static_cast<unsigned long>(len >> 32));
#elif defined(__linux__)
  return ::sync_file_range(fd, static_cast<off64_t>(off),
                           static_cast<off64_t>(len), flags);
#else
  (void)fd;
  (void)off;
  (void)len;
  (void)flags;
  return 0;
#endif
}

// Drop the (clean) page-cache pages of [off, off+len); len 0 = to EOF.
// Returns 0 or an errno value — posix_fadvise returns its error and does
// NOT set errno. Silently skips dirty pages, which is why callers write
// back and wait first.
inline int EvictPages(int fd, std::uint64_t off, std::uint64_t len) {
#if defined(__linux__)
  return ::posix_fadvise64(fd, static_cast<off64_t>(off),
                           static_cast<off64_t>(len), POSIX_FADV_DONTNEED);
#else
  (void)fd;
  (void)off;
  (void)len;
  return 0;
#endif
}

// Write back [off, off+len), wait for it, then evict it. The
// WAIT_BEFORE|WRITE|WAIT_AFTER triple is deliberate: only the triple
// upgrades to WB_SYNC_ALL, guaranteeing the range is clean before the
// DONTNEED that would otherwise skip its dirty pages. Returns 0 or errno.
inline int WritebackAndEvict(int fd, std::uint64_t off, std::uint64_t len) {
#if defined(__linux__)
  if (SyncRange(fd, off, len,
                SYNC_FILE_RANGE_WAIT_BEFORE | SYNC_FILE_RANGE_WRITE |
                    SYNC_FILE_RANGE_WAIT_AFTER) != 0) {
    return errno;
  }
  return EvictPages(fd, off, len);
#else
  (void)fd;
  (void)off;
  (void)len;
  return 0;
#endif
}

// fsync a path (a file, or a directory with O_DIRECTORY) to push it to
// physical media. Reopening read-only is enough — fsync flushes dirty pages
// regardless of the open mode. Returns 0 or the errno of the first call
// that failed, never thrown: the caller says what it costs (for a part,
// that its durability is unconfirmed), and needs the errno to judge it.
inline int FsyncPath(const std::string& path, int extra_open_flags) {
  const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC | extra_open_flags);
  if (fd < 0) return errno;
  const int synced = ::fsync(fd) == 0 ? 0 : errno;
  const int closed = ::close(fd) == 0 ? 0 : errno;
  return synced ? synced : closed;
}

inline std::string ParentDir(const std::string& path) {
  const std::size_t slash = path.find_last_of('/');
  return slash == std::string::npos ? "."
         : slash == 0               ? "/"
                                    : path.substr(0, slash);
}

// Bounded writeback the kernel or filesystem does not offer. It costs the
// optimization alone: nothing was handed to writeback, so nothing was lost.
inline bool WritebackUnsupported(int error) {
  return error == ENOSYS || error == EINVAL || error == EOPNOTSUPP;
}

// Make the directory entry naming `path` durable: after a part is written
// (so a power cut cannot lose its name) or removed (so it cannot come back).
// 0 or errno, as FsyncPath.
inline int FsyncDirEntry(const std::string& path) {
  return FsyncPath(ParentDir(path), O_DIRECTORY);
}

}  // namespace visio_schema::mcap::file_sync
