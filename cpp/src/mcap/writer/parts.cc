// parts — McapWriter's parts: built with the writer, each opened (and
// tried again when it will not open), cut, rolled to the next and closed.
#include "visio_schema/mcap/writer.hpp"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_set>
#include <utility>

// MCAP_IMPLEMENTATION pulls the writer .inl into this single TU.
#define MCAP_IMPLEMENTATION
#include "mcap/writer/vendored_mcap.hpp"

#include "visio_schema/log.hpp"
#include "visio_schema/mcap/recording_crypto.hpp"

#include "mcap/part_outcome.hpp"
#include "mcap/span_readback.hpp"
#include "mcap/writer/part_file.hpp"

namespace visio_schema::mcap {

namespace {

::mcap::McapWriterOptions MakeOptions() {
  ::mcap::McapWriterOptions opts("");  // empty profile: plain protobuf channels
  opts.compression = ::mcap::Compression::None;
  // Chunk CRC stays at upstream's default (on): it is the only check that
  // sees a flipped bit inside a payload. Record framing and the message
  // index survive damage confined to message bodies, so without it a part
  // whose chunks were altered at rest still reads as clean. The CRC is
  // accumulated as each record enters the chunk buffer, while the bytes are
  // still hot in cache.
  return opts;
}

// Insert "_NNNN" before the file extension: run.mcap -> run_0000.mcap.
// 4-digit zero-pad: parts stay lexicographically ordered through 9999. (At 3
// digits, part 1000 -> "_1000" sorts before "_999", breaking the chronological
// order the uploader/playback rely on once a session exceeds 999 parts.)
std::string NumberedPart(const std::string& path, std::size_t index) {
  char tag[16];
  std::snprintf(tag, sizeof(tag), "_%04zu", index);
  const std::size_t slash = path.find_last_of('/');
  const std::size_t dot = path.find_last_of('.');
  const bool has_ext = dot != std::string::npos &&
                       (slash == std::string::npos || dot > slash);
  if (!has_ext) return path + tag;
  return path.substr(0, dot) + tag + path.substr(dot);
}

}  // namespace

McapWriter::McapWriter(std::string_view path, std::uint64_t max_bytes,
                       double max_duration_s, bool rotate_on_keyframe,
                       std::int64_t pair_guard_ns,
                       std::uint64_t sync_span_bytes,
                       std::optional<RecordingKey> recording_key,
                       McapReadbackOptions readback,
                       std::shared_ptr<WriterStorage> storage,
                       std::chrono::milliseconds checkpoint_interval,
                       std::optional<McapRotationPolicy> rotation_policy)
    : rotation_policy_(std::move(rotation_policy)), base_path_(path),
      max_bytes_(max_bytes),
      max_duration_ns_(static_cast<std::int64_t>(max_duration_s * 1e9)),
      rotating_(max_bytes > 0 || max_duration_s > 0.0),
      rotate_on_keyframe_(rotate_on_keyframe),
      pair_guard_ns_(pair_guard_ns),
      sync_span_bytes_(sync_span_bytes),
      recording_key_(std::move(recording_key)),
      storage_(storage ? std::move(storage) : LocalWriterStorage()),
      checkpoint_interval_(checkpoint_interval),
      part_(std::make_unique<PartOutcome>()),
      readback_(readback.ring_bytes > 0
                    ? std::make_unique<SpanReadback>(std::move(readback))
                    : nullptr) {
  if (rotation_policy_) {
    if (rotation_policy_->tolerance_ns < 0 || rotation_policy_->max_staged_bytes == 0 ||
        rotation_policy_->max_wait.count() < 0 || rotation_policy_->start_wait.count() <= 0)
      throw std::invalid_argument("invalid MCAP rotation policy");
    std::unordered_set<std::uint32_t> ids(rotation_policy_->video_ids.begin(),
                                          rotation_policy_->video_ids.end());
    if (ids.size() != rotation_policy_->video_ids.size())
      throw std::invalid_argument("duplicate selected video id");
    if(!ids.empty()) {
      start_status_.store(StartStatus::Waiting);
      start_deadline_=std::chrono::steady_clock::now()+rotation_policy_->start_wait;
    }
    RecordingNonce nonce{};
    if (!RandomNonce(&nonce)) throw std::runtime_error("recording identity entropy unavailable");
    recording_id_ = HexOf(nonce.data(), nonce.size());
  }
  OpenPart();
}

McapWriter::~McapWriter() {
  try { Close(); }
  catch (...) { log::Write(log::Severity::kError, "McapWriter: destructor finalize failed"); }
}

std::string McapWriter::PartPath() const {
  return rotating_ ? NumberedPart(base_path_, part_index_) : base_path_;
}

// A part that will not open for a reason storage may get past is tried
// again, up to kPartOpenAttempts in all on a doubling wait, its failed file
// removed (it holds no recorded data). The retry keeps the part's number --
// a reader takes a gap in part numbers for a lost part, and resets a
// continuation chain at it -- unless the name is taken: by another file
// (EEXIST), tried again at once under the next number, which costs no
// attempt (up to kPartNamesSkipped); or by the failed file, which could not
// be removed. A name is never reused over a file. Only a failure no part
// could escape (EndsRecording), a taken name with no next number (not
// rotating), or every attempt failing ends the recording: the throw the
// endpoint latches as write_failed().
void McapWriter::OpenPart() {
  auto wait = kPartOpenFirstWait;
  int attempts = 0;
  int names_skipped = 0;
  for (;;) {
    const std::string tried = PartPath();
    const PartAttempt opened =
        TryOpenPart(attempts + 1 == kPartOpenAttempts);
    if (!opened.failure.error) return;
    attempts += opened.costs_attempt ? 1 : 0;
    names_skipped += opened.costs_attempt ? 0 : 1;
    if (!OpenMayRetry(opened, attempts, names_skipped))
      EndOpening(tried, opened.failure);
    if (opened.name_taken) ++part_index_;
    NotePartRetry(tried, opened.failure);
    if (!opened.costs_attempt) continue;
    std::this_thread::sleep_for(wait);
    wait *= 2;
  }
}

bool McapWriter::OpenMayRetry(const PartAttempt& opened, int attempts,
                              int names_skipped) const {
  if (EndsRecording(opened.failure.error)) return false;
  if (opened.name_taken && !rotating_) return false;  // no next name
  return attempts < kPartOpenAttempts && names_skipped <= kPartNamesSkipped;
}

// No part could be opened: the recording ends here (write_failed()), and a
// volume that refused it says so (storage_refused()).
void McapWriter::EndOpening(const std::string& tried, const IoResult& failed) {
  if (EndsRecording(failed.error)) part_->Note(failed);
  ended_.store(true, std::memory_order_relaxed);
  throw std::runtime_error("McapWriter: cannot open " + tried + ": " +
                           IoStepName(failed.step) + " failed: " +
                           std::strerror(failed.error));
}

void McapWriter::NotePartRetry(const std::string& tried,
                               const IoResult& failed) {
  part_open_retries_.fetch_add(1, std::memory_order_relaxed);
  if (part_retry_said_) return;
  part_retry_said_ = true;
  log::Write(log::Severity::kWarning,
             "McapWriter: %s: %s failed (%s): trying %s (retries for this "
             "part are counted, not logged again)",
             tried.c_str(), IoStepName(failed.step),
             std::strerror(failed.error), PartPath().c_str());
}

// Remove `path` and, when `sync`, make the removal durable. The failure,
// if any.
IoResult McapWriter::RemoveDurably(const std::string& path, bool sync) {
  const IoResult removed = storage_->Remove(path);
  if (!removed || !sync) return removed;
  return storage_->SyncParent(path);
}

// An empty part that could not be dropped cleanly: said and counted
// (storage_fault). It holds no records, so no data rides on it.
void McapWriter::NoteDropFailure(const std::string& path,
                                 const IoResult& failed) {
  drop_failures_.fetch_add(1, std::memory_order_relaxed);
  log::Write(log::Severity::kWarning,
             "McapWriter: could not drop empty part %s: %s (%s)",
             path.c_str(), IoStepName(failed.step),
             std::strerror(failed.error));
}

// A file made for a part that never opened holds no recorded data: gone,
// so it neither lingers nor reads as a part. Its removal is made durable
// only when no attempt follows (`last`): one that does re-creates the name.
// True once the name is free again.
bool McapWriter::DropUnopenedPart(const std::string& path, bool last) {
  const IoResult removed = RemoveDurably(path, last);
  if (removed) return true;
  NoteDropFailure(path, removed);
  return removed.step != IoStep::remove;  // only its barrier failed
}

McapWriter::PartAttempt McapWriter::TryOpenPart(bool last) {
  // Each part re-registers its own schemas/channels so it stands alone.
  schema_ids_.clear();
  channel_ids_.clear();
  // Legacy parts warm up independently. A continuation chain only warms up
  // local cameras once, including cameras whose first IDR arrives after a cut.
  if (!rotation_policy_) primed_video_channels_.clear();
  part_max_video_ts_ = INT64_MIN;
  part_bytes_ = 0;
  part_write_failure_logged_ = false;
  part_start_ = std::chrono::steady_clock::now();
  const std::string p = PartPath();
  part_->Begin(p);

  // Own the fd (O_CLOEXEC) via our IWritable instead of upstream's fopen(),
  // then hand it to the writer through the open(IWritable&) overload. See
  // CloexecFileWriter for why (recording fds must not leak into forked Wi-Fi
  // daemons). The writable is stored in file_ (declared before writer_) so it
  // outlives the writer that holds a raw pointer to it.
  // The read-back learns the part before its first byte (the VREC header
  // is written inside open()).
  if (readback_) readback_->BeginPart(p);
  auto fw = std::make_unique<CloexecFileWriter>(
      sync_span_bytes_, recording_key_ ? &*recording_key_ : nullptr,
      readback_.get(), part_.get(), storage_, checkpoint_interval_);
  const PartOpening opening = fw->open(p);
  if (opening.failure.error) {
    PartAttempt failed{opening.failure};
    if (opening.created) {
      const bool last_try = last || EndsRecording(opening.failure.error);
      failed.name_taken = !DropUnopenedPart(p, last_try);
      return failed;
    }
    // Another file holds the name: tried again at once, under the next.
    failed.name_taken = opening.failure.error == EEXIST;
    failed.costs_attempt = !failed.name_taken;
    return failed;
  }
  writer_ = std::make_unique<::mcap::McapWriter>();
  writer_->open(*fw, MakeOptions());
  file_ = std::move(fw);
  part_retry_said_ = false;  // the next part's retries are said again
  WriteStoredMetadata();  // re-emit capture metadata so each part stands alone
  WriteRotationMetadata();
  return {};
}

// A part that may have lost bytes (PartOutcome::lost_bytes) is cut at its
// next record: its byte count, offsets and keystream position no longer
// match its file, so nothing more may go into it. The cut part is kept
// (storage repairs its torn tail). A writer that does not rotate has no
// next part to cut to.
bool McapWriter::MustCut() const {
  return rotating_ && part_bytes_ > 0 && part_->lost_bytes();
}

bool McapWriter::ShouldRoll() const {
  // Never roll an empty part (a stale duration would spin out empty files).
  if (part_bytes_ == 0) return false;
  if (MustCut()) return true;
  if (max_bytes_ > 0 && part_bytes_ >= max_bytes_) return true;
  if (max_duration_ns_ > 0) {
    const auto elapsed = std::chrono::steady_clock::now() - part_start_;
    const auto ns =
        std::chrono::duration_cast<std::chrono::nanoseconds>(elapsed).count();
    if (ns >= max_duration_ns_) return true;
  }
  return false;
}

void McapWriter::CloseCurrentPart() {
  const std::string p = PartPath();  // capture before close, while state is live
  // A part is opened eagerly the moment its predecessor crosses the cap, so one
  // can close having never accepted a record: the keyframe gate below drops the
  // very frame that triggered the roll, and every frame after it, until that
  // stream's next IDR — and a write path that fails leaves nothing at all, not
  // even the header. Either way the file carries no records, which no reader
  // will open, sitting in a session directory that is otherwise sound. Drop it
  // instead of shipping it.
  //
  // Part 0 is retained for a short session, except when the synchronized
  // start never completed: then there is no recording to hand a reader.
  const bool pending_start = rotation_policy_ && start_status_.load() != StartStatus::Ready;
  const bool drop_part =
      (part_index_ > 0 || pending_start) && part_bytes_ == 0 && !part_->failed();
  if (drop_part) part_->Dropping();
  writer_->close();
  physical_closed_ += part_->result().accepted;
  if (drop_part) {
    // Before any fsync or read-back post: syncing a file about to be unlinked
    // only makes the discarded state durable, and a tail posted here names a
    // path that no longer exists by the time the reader reaches it (an ENOENT
    // counted as a read failure). Finish() accounts for an unposted tail.
    // A failure its close met is the drop's to say, removed or not.
    const IoResult closed = part_->result();
    const IoResult removed = RemoveDurably(p, true);
    if (closed.error || removed.error)
      NoteDropFailure(p, closed.error ? closed : removed);
    return;
  }
  const auto synced = storage_->SyncParent(p);
  if (!synced) part_->Note(synced);
  part_->Complete(storage_->Completed(p, part_->result()));
  // Only now is the tail on the media: post it for read-back.
  if (readback_) readback_->CommitTail();
}

// The closed part's mcap writer goes with it, so a next part that cannot
// be opened (OpenPart throws) leaves nothing a later Close() could close,
// remove or complete a second time.
void McapWriter::Roll() {
  if (!writer_) return;
  CloseCurrentPart();
  writer_.reset();
  if (part_->refused()) return;  // the volume takes nothing more
  ++part_index_;
  OpenPart();
}

}  // namespace visio_schema::mcap
