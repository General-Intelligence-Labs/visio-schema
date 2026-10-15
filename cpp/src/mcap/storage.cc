#include "visio_schema/mcap/storage.hpp"

#include <cerrno>
#include <cstdio>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>

#include "file_sync.hpp"

namespace visio_schema::mcap {
namespace {
IoResult Error(IoStep step) { return {step, errno ? errno : EIO, 0, true}; }

class LocalFile final : public OutputFile {
 public:
  explicit LocalFile(std::FILE* file) : file_(file) {}
  ~LocalFile() override { if (file_) std::fclose(file_); }
  IoResult Append(const void* bytes, std::size_t size) override {
    if (!file_) return {IoStep::append, EBADF};
    errno = 0;
    const auto n = std::fwrite(bytes, 1, size, file_);
    return {IoStep::append, n == size ? 0 : (errno ? errno : EIO), n, n != size};
  }
  IoResult Flush() override {
    if (!file_) return {IoStep::flush, EBADF};
    return std::fflush(file_) == 0 ? IoResult{} : Error(IoStep::flush);
  }
  IoResult Checkpoint() override {
    const auto flushed = Flush();
    if (!flushed) return flushed;
    return ::fsync(::fileno(file_)) == 0 ? IoResult{} : Error(IoStep::checkpoint);
  }
  IoResult Close() override {
    if (!file_) return {};
    struct stat64 st{};
    const bool measured = ::fstat64(::fileno(file_), &st) == 0;
    const int measure_error = measured ? 0 : (errno ? errno : EIO);
    auto* file = file_;
    file_ = nullptr;  // fclose consumes it even when it reports an error
    auto result = std::fclose(file) == 0 ? IoResult{} : Error(IoStep::close);
    if (measure_error) result = {IoStep::close, measure_error, 0, true};
    result.accepted = measured ? static_cast<std::uint64_t>(st.st_size) : 0;
    return result;
  }
  int Writeback(std::uint64_t off, std::uint64_t len, bool wait) override {
    if (!file_) return EBADF;
    if (wait) return file_sync::WritebackAndEvict(::fileno(file_), off, len);
#ifdef __linux__
    return file_sync::SyncRange(::fileno(file_), off, len, SYNC_FILE_RANGE_WRITE) == 0 ? 0 : errno;
#else
    return 0;
#endif
  }
 private:
  std::FILE* file_;
};

class LocalStorage final : public WriterStorage {
 public:
  explicit LocalStorage(bool exclusive) : exclusive_(exclusive) {}
  std::unique_ptr<OutputFile> Create(const std::string& path, IoResult& result) override {
    const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | (exclusive_ ? O_EXCL : O_TRUNC) | O_CLOEXEC, 0644);
    if (fd < 0) { result = Error(IoStep::open); return {}; }
    auto* file = ::fdopen(fd, "wb");
    if (!file) {
      result = Error(IoStep::open);
      ::close(fd);
      return {};
    }
    ::setvbuf(file, nullptr, _IOFBF, 16 * 1024);
    result = {};
    return std::make_unique<LocalFile>(file);
  }
  IoResult SyncParent(const std::string& path) override {
    return file_sync::FsyncDirEntry(path) ? IoResult{} : Error(IoStep::namespace_sync);
  }
  IoResult Remove(const std::string& path) override {
    return ::unlink(path.c_str()) == 0 || errno == ENOENT ? IoResult{} : Error(IoStep::remove);
  }
 private:
  bool exclusive_;
};
}  // namespace

std::shared_ptr<WriterStorage> LocalWriterStorage(bool exclusive) {
  static auto storage = std::make_shared<LocalStorage>(false);
  static auto create_only = std::make_shared<LocalStorage>(true);
  return exclusive ? create_only : storage;
}
}  // namespace visio_schema::mcap
