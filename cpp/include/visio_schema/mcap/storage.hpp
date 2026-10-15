#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace visio_schema::mcap {

// Storage policy belongs to the device. MCAP/VREC encoding does not need a
// descriptor or knowledge of mounts, reservations, receipts or scheduling.
enum class IoStep { none, open, read, append, flush, checkpoint, close, namespace_sync, remove };
struct IoResult {
  IoStep step = IoStep::none;
  int error = 0;
  std::uint64_t accepted = 0;
  bool uncertain = false;
  explicit operator bool() const { return error == 0; }
};

class OutputFile {
 public:
  virtual ~OutputFile() = default;
  virtual IoResult Append(const void* bytes, std::size_t size) = 0;
  virtual IoResult Flush() = 0;
  virtual IoResult Checkpoint() = 0;
  virtual IoResult Close() = 0;
  // Bounded Linux writeback; an optimization, separate from durability.
  virtual int Writeback(std::uint64_t off, std::uint64_t len, bool wait) = 0;
};

class WriterStorage {
 public:
  virtual ~WriterStorage() = default;
  virtual std::unique_ptr<OutputFile> Create(const std::string& path, IoResult& result) = 0;
  virtual IoResult SyncParent(const std::string& path) = 0;
  virtual IoResult Remove(const std::string& path) = 0;
  // Called after checked close and namespace barriers, even on failure.
  // A device can publish a finalization receipt here; its failure is a save
  // failure too. 'accepted' includes a VREC header, when present.
  virtual IoResult Completed(const std::string&, const IoResult& result) { return result; }
};

// The default for host users. Device consumers inject their storage authority.
std::shared_ptr<WriterStorage> LocalWriterStorage(bool exclusive = false);

}  // namespace visio_schema::mcap
