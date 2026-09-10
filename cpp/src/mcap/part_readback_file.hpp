// PartReadbackFile — the read-back's own handles on one part file. Reads
// are O_DIRECT where the file system allows it, so they are served by the
// medium and not by the page cache the writer just filled, and read-only,
// so a card that remounts read-only mid-recording still verifies; where
// O_DIRECT is refused (tmpfs, some FUSE mounts) a buffered read with
// explicit eviction stands in, logged once per part. A rewrite opens its
// own writable fd only when needed: O_DIRECT for whole pages, buffered +
// writeback + evict for a partial tail. Private to src/mcap.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace visio_schema::mcap {

class PartReadbackFile {
 public:
  enum class ReadOutcome { kOk, kShortFile, kError };

  PartReadbackFile() = default;
  ~PartReadbackFile();
  PartReadbackFile(const PartReadbackFile&) = delete;
  PartReadbackFile& operator=(const PartReadbackFile&) = delete;

  // Open `path` for reading (a no-op when it is already the open file).
  // False, logged once per path, when it cannot be opened at all; the
  // errno is in last_errno().
  bool Open(const std::string& path);
  void Close();
  bool is_open() const { return read_fd_ >= 0; }
  const std::string& path() const { return path_; }
  int last_errno() const { return last_errno_; }

  // Read exactly `len` bytes at page-aligned `off` into `buf`, which must
  // be page-aligned with room for `len` rounded up to a page (O_DIRECT
  // reads whole blocks; a tail past EOF comes back short and is fine).
  // kShortFile when the file ends first; kError (errno in last_errno())
  // when the read itself failed.
  ReadOutcome ReadPiece(std::uint64_t off, void* buf, std::size_t len);
  // Write `len` bytes at page-aligned `off` from page-aligned `buf`.
  // False (errno in last_errno()) when the write fails.
  bool WritePiece(std::uint64_t off, const void* buf, std::size_t len);

 private:
  int OpenFd(int flags);
  int WriteFd(bool direct);

  std::string path_;
  int read_fd_ = -1;
  bool read_direct_ = false;
  int write_direct_fd_ = -1;    // opened on the first whole-page rewrite
  int write_buffered_fd_ = -1;  // opened on the first tail rewrite
  int last_errno_ = 0;
  bool open_failure_logged_ = false;
};

}  // namespace visio_schema::mcap
