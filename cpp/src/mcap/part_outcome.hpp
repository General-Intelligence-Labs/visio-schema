// part_outcome — what the storage steps of one recorded part came to, and
// whether the recording can go on past them. Private to src/mcap.
//
// A failed step belongs to its part: the part is kept, but its durability
// is not confirmed -- storage completion is told so, and decides what the
// part waits for. Its first failure is said once, and the recording carries
// on into the next part; one that may have lost bytes (LosesBytes) also cuts
// the part there (McapWriter). Nothing handed to storage is discarded.
// Only a failure no later part could escape ends the recording: the volume
// is read-only, full, or its device is gone (EndsRecording, storage.hpp).
// The other way a recording ends is a next part that cannot be opened,
// which McapWriter throws.
//
// Written by the writer thread alone; refused() and unconfirmed_parts() may
// be polled from any thread.
#pragma once

#include <atomic>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <string>
#include <utility>

#include "visio_schema/log.hpp"
#include "visio_schema/mcap/storage.hpp"

namespace visio_schema::mcap {

// The words a failed step is said with.
inline const char* IoStepName(IoStep step) {
  switch (step) {
    case IoStep::open: return "open";
    case IoStep::read: return "read";
    case IoStep::append: return "write";
    case IoStep::flush: return "flush";
    case IoStep::checkpoint: return "checkpoint";
    case IoStep::writeback: return "writeback";
    case IoStep::close: return "close";
    case IoStep::namespace_sync: return "directory sync";
    case IoStep::remove: return "remove";
    case IoStep::complete: return "completion";
    case IoStep::none: break;
  }
  return "storage step";
}

class PartOutcome {
 public:
  // A new part, `path`: nothing has failed in it yet.
  void Begin(std::string path) {
    path_ = std::move(path);
    first_failure_ = {};
    lossy_failure_ = {};
    closed_bytes_ = 0;
    dropping_ = false;
  }

  // The part holds no records and is being dropped: what fails on its way
  // out is said as the drop's (McapWriter), not as a kept part's.
  void Dropping() { dropping_ = true; }

  // A step of the current part failed; `cause`, when given, is said in
  // place of the step and its errno. The part keeps its first failure, and
  // its first that may have lost bytes (lost_bytes), each said once. One
  // the volume will not get past also ends the recording, said once for it.
  void Note(IoResult failed, const std::string& cause = {}) {
    failed = Explained(failed);
    if (EndsRecording(failed.error)) Refuse(failed);
    const bool first = !this->failed();
    const bool newly_lossy = LosesBytes(failed.step) && !lost_bytes();
    if (first) first_failure_ = failed;
    if (newly_lossy) lossy_failure_ = failed;
    if (refused() || dropping_ || !(first || newly_lossy)) return;
    Say(failed, cause);
  }

  // `failed` as a failure: one storage reported without an errno broke the
  // storage contract, said once as such and taken as EIO.
  IoResult Explained(IoResult failed) {
    if (failed.error != 0) return failed;
    if (!said_unexplained_) {
      said_unexplained_ = true;
      log::Write(log::Severity::kError,
                 "McapWriter: %s: storage reported a failed %s without an "
                 "errno (a storage contract violation); taken as EIO",
                 path_.c_str(), IoStepName(failed.step));
    }
    return {failed.step, EIO, failed.accepted, true};
  }

  // The part's file is closed and measured `bytes` (a VREC header included).
  void Closed(std::uint64_t bytes) { closed_bytes_ = bytes; }

  // What the part came to, and what storage completion is told: its first
  // failure that may have lost bytes, else its first failure (success while
  // there is none), and the bytes its file held at close.
  IoResult result() const {
    IoResult part = lost_bytes() ? lossy_failure_ : first_failure_;
    part.accepted = closed_bytes_;
    return part;
  }

  // Storage completion answered `completed`; a part whose durability is not
  // confirmed is counted.
  void Complete(const IoResult& completed) {
    if (!completed) Note(completed);
    if (failed())
      unconfirmed_parts_.fetch_add(1, std::memory_order_relaxed);
  }

  bool failed() const { return first_failure_.error != 0; }
  // A step may have lost bytes: the part's offsets no longer match its
  // file, so nothing more may go into it.
  bool lost_bytes() const { return lossy_failure_.error != 0; }
  // Storage refused a write for good: nothing more is written.
  bool refused() const { return refused_.load(std::memory_order_relaxed); }
  std::uint64_t unconfirmed_parts() const {
    return unconfirmed_parts_.load(std::memory_order_relaxed);
  }

 private:
  void Say(const IoResult& failed, const std::string& cause) const {
    const std::string what =
        !cause.empty() ? cause
                       : std::string(IoStepName(failed.step)) + " failed (" +
                             std::strerror(failed.error) + ")";
    const char* consequence =
        LosesBytes(failed.step)
            ? "bytes may be lost: the part is cut here and kept, its "
              "durability not confirmed; recording continues"
            : "the part is kept, its durability not confirmed; recording "
              "continues";
    log::Write(log::Severity::kWarning, "McapWriter: %s: %s: %s",
               path_.c_str(), what.c_str(), consequence);
  }
  void Refuse(const IoResult& failed) {
    if (refused_.exchange(true, std::memory_order_relaxed)) return;
    log::Write(log::Severity::kError,
               "McapWriter: %s: %s failed (%s): storage takes no more "
               "writes; the part is kept and the recording ends",
               path_.c_str(), IoStepName(failed.step),
               std::strerror(failed.error));
  }

  std::string path_;
  IoResult first_failure_;
  IoResult lossy_failure_;
  std::uint64_t closed_bytes_ = 0;
  bool dropping_ = false;
  bool said_unexplained_ = false;
  std::atomic<bool> refused_{false};
  std::atomic<std::uint64_t> unconfirmed_parts_{0};
};

}  // namespace visio_schema::mcap
