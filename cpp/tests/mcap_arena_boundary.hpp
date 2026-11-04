// Test-private gates around real storage and the real arena's madvise call.
#pragma once

#include <pthread.h>

#include <array>
#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "visio_schema/mcap/storage.hpp"

namespace mcap_arena_test {

// Completion order is the assertion; waits only bound a broken handshake.
class Gate {
 public:
  void Arm();
  void Hold();
  bool WaitEntered();
  bool WaitSent();
  void Sent();
  void Release();
  bool SentBeforeRelease() const;

 private:
  mutable std::mutex mu_;
  std::condition_variable cv_;
  bool armed_ = false, entered_ = false, released_ = false;
  unsigned order_ = 0, sent_order_ = 0, release_order_ = 0;
};

int ObserveWait(pthread_cond_t* condition, pthread_mutex_t* mutex);

// Only designated, prewarmed Send callers install a scope. Entry is observed
// while the caller still owns the real endpoint mutex; no wait is replaced.
class WaitProbe {
 public:
  struct Observation {
    pthread_cond_t* condition = nullptr;
    pthread_mutex_t* mutex = nullptr;
    std::thread::id producer;
    std::size_t entries = 0, returns = 0;
    bool same_identity = true, finished = false;
    std::optional<int> result;
  };
  class Scope {
   public:
    Scope(WaitProbe& probe, std::size_t id);
    ~Scope();
    Scope(const Scope&) = delete;
    Scope& operator=(const Scope&) = delete;

   private:
    friend int ObserveWait(pthread_cond_t*, pthread_mutex_t*);
    WaitProbe& probe_;
    const std::size_t id_;
    Scope* previous_;
  };
  bool WaitEntered(std::size_t producers);
  Observation Snapshot(std::size_t id) const;

 private:
  friend int ObserveWait(pthread_cond_t*, pthread_mutex_t*);
  void Enter(std::size_t id, pthread_cond_t* condition,
             pthread_mutex_t* mutex);
  void Returned(std::size_t id, int result);
  void Finished(std::size_t id);
  mutable std::mutex mu_;
  std::condition_variable cv_;
  std::array<Observation, 2> observations_;
};

// Forwards every operation to LocalWriterStorage; only Append can be gated.
class Storage final : public visio_schema::mcap::WriterStorage {
 public:
  using IoResult = visio_schema::mcap::IoResult;
  using OutputFile = visio_schema::mcap::OutputFile;
  std::unique_ptr<OutputFile> Create(const std::string& path,
                                     IoResult& result) override;
  IoResult SyncParent(const std::string& path) override;
  IoResult Remove(const std::string& path) override;
  IoResult Completed(const std::string& path, const IoResult& result) override;
  Gate append;
  std::atomic<bool> fail_append{false};

 private:
  class File;
  std::shared_ptr<WriterStorage> inner_ =
      visio_schema::mcap::LocalWriterStorage();
};

// Installed only in the separate --wrap=madvise target. The endpoint and its
// callbacks must finish before this scope leaves; no writer/arena is doubled.
class ReclaimProbe {
 public:
  explicit ReclaimProbe(std::function<std::uint64_t()> writes, int error = 0);
  ~ReclaimProbe();
  ReclaimProbe(const ReclaimProbe&) = delete;
  ReclaimProbe& operator=(const ReclaimProbe&) = delete;
  int Call(void* address, std::size_t length, int advice);
  Gate reclaim;
  Gate returned;

  // Read after endpoint Stop; the entry snapshot is also stable once entered.
  const std::vector<char>& entry_bytes() const { return entry_bytes_; }
  std::uint64_t writes_at_entry() const { return writes_at_entry_; }
  unsigned calls() const;
  bool checked_after_send() const { return checked_after_send_; }
  bool range_unchanged() const { return range_unchanged_; }
  int syscall_result() const { return syscall_result_; }
  const char* address() const { return address_; }

 private:
  int Advise(void* address, std::size_t length, int advice);
  std::function<std::uint64_t()> writes_;
  const int injected_error_;
  std::vector<char> entry_bytes_;
  const char* address_ = nullptr;
  std::uint64_t writes_at_entry_ = 0;
  bool checked_after_send_ = false, range_unchanged_ = false;
  int syscall_result_ = -1;
  std::atomic<unsigned> calls_{0};
};

}  // namespace mcap_arena_test
