#pragma once

#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace visio_schema::mcap {

// Storage policy belongs to the device. MCAP/VREC encoding does not need a
// descriptor or knowledge of mounts, reservations, receipts or scheduling.
// `complete` is the storage's own completion step (WriterStorage::Completed).
enum class IoStep {
  none, open, read, append, flush, checkpoint, close, namespace_sync, remove,
  writeback, complete
};
struct IoResult {
  IoStep step = IoStep::none;
  int error = 0;
  std::uint64_t accepted = 0;
  bool uncertain = false;
  explicit operator bool() const { return error == 0; }
};

// A failed step no later part could escape: the volume is read-only, full,
// or its device is gone. A recording ends on one of these. Any other
// failure costs only the part it happened in (see McapWriter).
inline bool EndsRecording(int error) {
  return error == EROFS || error == ENOSPC || error == ENODEV;
}

// A failed step that may have lost bytes the writer had already counted as
// written: a write, a flush or close of buffered bytes, and a checkpoint or
// writeback (the kernel marks pages whose write-out failed clean, so a later
// sync succeeds over them). The part's offsets no longer match its file:
// the writer cuts it there, and storage keeps it as torn.
inline bool LosesBytes(IoStep step) {
  return step == IoStep::append || step == IoStep::flush ||
         step == IoStep::close || step == IoStep::checkpoint ||
         step == IoStep::writeback;
}

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
  // A failed Create says why in `result` and leaves no file behind.
  virtual std::unique_ptr<OutputFile> Create(const std::string& path, IoResult& result) = 0;
  virtual IoResult SyncParent(const std::string& path) = 0;
  virtual IoResult Remove(const std::string& path) = 0;
  // Called after checked close and namespace barriers, even on failure.
  // A device can publish a finalization receipt here; its failure is a save
  // failure too. 'accepted' includes a VREC header, when present. A failed
  // `result` carries the part's first failed step: the part's durability is
  // unconfirmed, and the storage keeps it (the writer never discards what
  // it handed over) and decides what confirms it later. A storage that
  // reports failures where they happen need not report that one again.
  virtual IoResult Completed(const std::string&, const IoResult& result) { return result; }
};

// The default for host users. Device consumers inject their storage authority.
std::shared_ptr<WriterStorage> LocalWriterStorage(bool exclusive = false);

}  // namespace visio_schema::mcap
