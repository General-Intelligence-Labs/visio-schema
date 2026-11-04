#include "mcap_arena_boundary.hpp"

#include <sys/mman.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <stdexcept>
#include <utility>

extern "C" int __real_madvise(void*, std::size_t, int);
extern "C" int __real_pthread_cond_wait(pthread_cond_t*, pthread_mutex_t*);

namespace mcap_arena_test {
namespace {
std::atomic<ReclaimProbe*> active{nullptr};
thread_local WaitProbe::Scope* active_wait = nullptr;
constexpr std::size_t kChunkBytes = 256u << 10;
}  // namespace

void Gate::Arm() {
  std::lock_guard<std::mutex> lock(mu_);
  armed_ = true;
}
void Gate::Hold() {
  std::unique_lock<std::mutex> lock(mu_);
  if (!armed_ || entered_) return;
  entered_ = true;
  cv_.notify_all();
  cv_.wait(lock, [&] { return released_; });
}
bool Gate::WaitEntered() {
  std::unique_lock<std::mutex> lock(mu_);
  return cv_.wait_for(lock, std::chrono::seconds(2), [&] { return entered_; });
}
bool Gate::WaitSent() {
  std::unique_lock<std::mutex> lock(mu_);
  return cv_.wait_for(lock, std::chrono::seconds(2),
                      [&] { return sent_order_; });
}
void Gate::Sent() {
  std::lock_guard<std::mutex> lock(mu_);
  sent_order_ = ++order_;
  cv_.notify_all();
}
void Gate::Release() {
  std::lock_guard<std::mutex> lock(mu_);
  if (released_) return;
  release_order_ = ++order_;
  released_ = true;
  cv_.notify_all();
}
bool Gate::SentBeforeRelease() const {
  std::lock_guard<std::mutex> lock(mu_);
  return sent_order_ && release_order_ && sent_order_ < release_order_;
}

WaitProbe::Scope::Scope(WaitProbe& probe, std::size_t id)
    : probe_(probe), id_(id), previous_(active_wait) {
  active_wait = this;
}
WaitProbe::Scope::~Scope() {
  active_wait = previous_;
  probe_.Finished(id_);
}
bool WaitProbe::WaitEntered(std::size_t producers) {
  std::unique_lock<std::mutex> lock(mu_);
  const auto entered = [&] {
    for (std::size_t i = 0; i < producers; ++i)
      if (!observations_.at(i).entries) return false;
    return true;
  };
  cv_.wait_for(lock, std::chrono::seconds(2), [&] {
    if (entered()) return true;
    for (std::size_t i = 0; i < producers; ++i)
      if (observations_.at(i).finished) return true;
    return false;
  });
  for (std::size_t i = 0; i < producers; ++i)
    if (observations_.at(i).finished) return false;
  return entered();
}
WaitProbe::Observation WaitProbe::Snapshot(std::size_t id) const {
  std::lock_guard<std::mutex> lock(mu_);
  return observations_.at(id);
}
void WaitProbe::Enter(std::size_t id, pthread_cond_t* condition,
                      pthread_mutex_t* mutex) {
  std::lock_guard<std::mutex> lock(mu_);
  auto& observation = observations_.at(id);
  const auto producer = std::this_thread::get_id();
  if (!observation.entries) {
    observation.condition = condition;
    observation.mutex = mutex;
    observation.producer = producer;
  } else if (observation.condition != condition ||
             observation.mutex != mutex || observation.producer != producer) {
    observation.same_identity = false;
  }
  ++observation.entries;
  cv_.notify_all();
}
void WaitProbe::Returned(std::size_t id, int result) {
  std::lock_guard<std::mutex> lock(mu_);
  auto& observation = observations_.at(id);
  ++observation.returns;
  observation.result = result;
}
void WaitProbe::Finished(std::size_t id) {
  std::lock_guard<std::mutex> lock(mu_);
  observations_.at(id).finished = true;
  cv_.notify_all();
}
int ObserveWait(pthread_cond_t* condition, pthread_mutex_t* mutex) {
  auto* scope = active_wait;
  if (!scope) return __real_pthread_cond_wait(condition, mutex);
  const int before = errno;
  scope->probe_.Enter(scope->id_, condition, mutex);
  errno = before;
  const int result = __real_pthread_cond_wait(condition, mutex);
  const int after = errno;
  scope->probe_.Returned(scope->id_, result);
  errno = after;
  return result;
}

class Storage::File final : public OutputFile {
 public:
  File(Gate& gate, const std::atomic<bool>& fail,
       std::unique_ptr<OutputFile> file)
      : gate_(gate), fail_(fail), file_(std::move(file)) {}
  IoResult Append(const void* bytes, std::size_t size) override {
    gate_.Hold();
    if (fail_.load())
      return {visio_schema::mcap::IoStep::append, ENOSPC, 0, false};
    return file_->Append(bytes, size);
  }
  IoResult Flush() override { return file_->Flush(); }
  IoResult Checkpoint() override { return file_->Checkpoint(); }
  IoResult Close() override { return file_->Close(); }
  int Writeback(std::uint64_t off, std::uint64_t len, bool wait) override {
    return file_->Writeback(off, len, wait);
  }

 private:
  Gate& gate_;
  const std::atomic<bool>& fail_;
  std::unique_ptr<OutputFile> file_;
};

std::unique_ptr<Storage::OutputFile> Storage::Create(const std::string& path,
                                                     IoResult& result) {
  auto file = inner_->Create(path, result);
  return file ? std::make_unique<File>(append, fail_append, std::move(file))
              : nullptr;
}
Storage::IoResult Storage::SyncParent(const std::string& path) {
  return inner_->SyncParent(path);
}
Storage::IoResult Storage::Remove(const std::string& path) {
  return inner_->Remove(path);
}
Storage::IoResult Storage::Completed(const std::string& path,
                                     const IoResult& result) {
  return inner_->Completed(path, result);
}

ReclaimProbe::ReclaimProbe(std::function<std::uint64_t()> writes, int error)
    : writes_(std::move(writes)), injected_error_(error) {
  ReclaimProbe* expected = nullptr;
  if (!active.compare_exchange_strong(expected, this))
    throw std::logic_error("arena reclaim probe already installed");
  reclaim.Arm();
}
ReclaimProbe::~ReclaimProbe() { active.store(nullptr); }
unsigned ReclaimProbe::calls() const { return calls_.load(); }

int ReclaimProbe::Call(void* address, std::size_t length, int advice) {
  if (advice != MADV_DONTNEED || length != kChunkBytes)
    return __real_madvise(address, length, advice);
  if (calls_.fetch_add(1) != 0) return Advise(address, length, advice);
  writes_at_entry_ = writes_();
  const auto* begin = static_cast<const char*>(address);
  address_ = begin;
  entry_bytes_.assign(begin, begin + length);
  reclaim.Hold();
  // Only inspect after the producer's completion fence: a failed handshake
  // can release the hang guard while a producer is still copying elsewhere.
  if (reclaim.SentBeforeRelease()) {
    checked_after_send_ = true;
    range_unchanged_ = std::memcmp(address, entry_bytes_.data(), length) == 0;
  }
  syscall_result_ = Advise(address, length, advice);
  returned.Sent();
  return syscall_result_;
}

int ReclaimProbe::Advise(void* address, std::size_t length, int advice) {
  if (!injected_error_) return __real_madvise(address, length, advice);
  errno = injected_error_;
  return -1;
}

int Dispatch(void* address, std::size_t length, int advice) {
  if (auto* probe = active.load()) return probe->Call(address, length, advice);
  return __real_madvise(address, length, advice);
}
}  // namespace mcap_arena_test

extern "C" int __wrap_madvise(void* address, std::size_t length, int advice) {
  return mcap_arena_test::Dispatch(address, length, advice);
}

extern "C" int __wrap_pthread_cond_wait(pthread_cond_t* condition,
                                       pthread_mutex_t* mutex) {
  return mcap_arena_test::ObserveWait(condition, mutex);
}
