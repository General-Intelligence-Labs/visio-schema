#include "part_readback_file.hpp"

#include <fcntl.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>

#include "file_sync.hpp"

#if defined(__linux__) && !defined(O_DIRECT)
#error "the read-back needs O_DIRECT to read the medium rather than the cache"
#endif

namespace visio_schema::mcap {

namespace {

// pread until `len` bytes are in, riding out short transfers. Returns the
// bytes read, or -errno when the read itself failed; fewer than `len`
// means the file ended first.
ssize_t PreadUpTo(int fd, std::uint64_t off, void* buf, std::size_t len) {
  auto* p = static_cast<std::uint8_t*>(buf);
  std::size_t done = 0;
  while (done < len) {
    const ssize_t n = ::pread(fd, p + done, len - done,
                              static_cast<off_t>(off + done));
    if (n < 0) {
      if (errno == EINTR) continue;
      return -errno;
    }
    if (n == 0) break;
    done += static_cast<std::size_t>(n);
  }
  return static_cast<ssize_t>(done);
}

bool PwriteAll(int fd, std::uint64_t off, const void* buf, std::size_t len) {
  auto* p = static_cast<const std::uint8_t*>(buf);
  std::size_t done = 0;
  while (done < len) {
    const ssize_t n = ::pwrite(fd, p + done, len - done,
                               static_cast<off_t>(off + done));
    if (n < 0) {
      if (errno == EINTR) continue;
      return false;
    }
    if (n == 0) {
      errno = EIO;
      return false;
    }
    done += static_cast<std::size_t>(n);
  }
  return true;
}

}  // namespace

PartReadbackFile::~PartReadbackFile() { Close(); }

int PartReadbackFile::OpenFd(int flags) {
  const int fd = ::open(path_.c_str(), flags | O_CLOEXEC);
  if (fd < 0) last_errno_ = errno;
  return fd;
}

bool PartReadbackFile::Open(const std::string& path) {
  if (is_open() && path == path_) return true;
  Close();
  path_ = path;
#if defined(__linux__)
  read_fd_ = OpenFd(O_RDONLY | O_DIRECT);
  read_direct_ = read_fd_ >= 0;
  if (read_fd_ < 0 && last_errno_ == EINVAL) {
    // The file system refuses direct I/O (tmpfs on older kernels, some
    // FUSE mounts): reads may be served from the page cache, so this is
    // a weaker check — say so, once per part.
    std::fprintf(stderr,
                 "mcap readback: %s: O_DIRECT unavailable, buffered "
                 "fallback\n",
                 path.c_str());
    read_fd_ = OpenFd(O_RDONLY);
  }
#else
  read_fd_ = OpenFd(O_RDONLY);
#endif
  if (read_fd_ < 0 && !open_failure_logged_) {
    open_failure_logged_ = true;
    std::fprintf(stderr, "mcap readback: cannot open %s: %s\n", path.c_str(),
                 std::strerror(last_errno_));
  }
  return read_fd_ >= 0;
}

void PartReadbackFile::Close() {
  for (int* fd : {&read_fd_, &write_direct_fd_, &write_buffered_fd_}) {
    if (*fd >= 0) ::close(*fd);
    *fd = -1;
  }
  read_direct_ = false;
  open_failure_logged_ = false;
  path_.clear();
}

int PartReadbackFile::WriteFd(bool direct) {
  int& fd = direct ? write_direct_fd_ : write_buffered_fd_;
#if defined(__linux__)
  if (fd < 0) fd = OpenFd(direct ? (O_RDWR | O_DIRECT) : O_RDWR);
#else
  if (fd < 0) fd = OpenFd(O_RDWR);
#endif
  return fd;
}

PartReadbackFile::ReadOutcome PartReadbackFile::ReadPiece(std::uint64_t off,
                                                          void* buf,
                                                          std::size_t len) {
  ssize_t got = 0;
  if (read_direct_) {
    got = PreadUpTo(read_fd_, off, buf, file_sync::RoundUpToPage(len));
  } else {
    // Best effort: a clean, evicted range makes even a buffered read go to
    // the medium. The sink already wrote these pages back, so they are
    // clean.
    file_sync::EvictPages(read_fd_, off, len);
    got = PreadUpTo(read_fd_, off, buf, len);
    file_sync::EvictPages(read_fd_, off, len);
  }
  if (got < 0) {
    last_errno_ = static_cast<int>(-got);
    return ReadOutcome::kError;
  }
  return static_cast<std::size_t>(got) >= len ? ReadOutcome::kOk
                                              : ReadOutcome::kShortFile;
}

bool PartReadbackFile::WritePiece(std::uint64_t off, const void* buf,
                                  std::size_t len) {
  // A partial tail cannot go through O_DIRECT (it would write whole blocks
  // past EOF); buffered, then the same writeback + evict the sink uses.
  const bool direct = read_direct_ && len % file_sync::kPageBytes == 0;
  const int fd = WriteFd(direct);
  if (fd < 0) return false;
  if (!PwriteAll(fd, off, buf, len)) {
    last_errno_ = errno;
    return false;
  }
  if (direct) return true;
  if (const int err = file_sync::WritebackAndEvict(fd, off, len)) {
    std::fprintf(stderr,
                 "mcap readback: %s: writeback after rewrite failed: %s\n",
                 path_.c_str(), std::strerror(err));
  }
  return true;
}

}  // namespace visio_schema::mcap
