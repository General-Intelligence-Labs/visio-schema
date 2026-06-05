// SerialWatchdog — decides when /dev/ttyGS0 must be reopened. The CDC-ACM gadget
// gives no host-detached signal (DTR/TIOCMGET is gadget-driver-dependent), so we
// infer it from:
//   - a USB gadget-state edge into "CONFIGURED" (the host re-enumerated)
//   - a drain stall: the outbox's pending bytes stay non-decreasing for N ticks
//     while USB looks healthy (the host closed its TTY without dropping USB)
//   - persistent client-not-open: the initial open never succeeded (e.g.
//     /dev/ttyGS0 appears asynchronously after gadget bind) — periodic retry.
//
// I/O-free by design so unit tests drive it deterministically; the caller does
// the sysfs read, the reopen, and the logging. Ported from
// umi_embedded/src/serial_watchdog.hpp (the proven RV1106 implementation).
#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>

namespace visio_schema::transport {

class SerialWatchdog {
 public:
  enum class Action {
    None,
    ReopenEdge,     // USB transitioned into "CONFIGURED"
    ReopenStalled,  // pending bytes stuck for >= kStallTicks while CONFIGURED
    ReopenRetry,    // no client open; periodic retry
  };

  static constexpr int kStallTicks = 3;            // ~6 s at 2 s ticks
  static constexpr std::int64_t kBaseRetryMs = 10000;    // 10 s
  static constexpr std::int64_t kMaxRetryMs = 300000;    // 5 min ceiling
  static constexpr int kBackoffStartAt = 3;        // back off after N failed retries

  // Inputs:
  //   usb_state   - "CONFIGURED" / "CONNECTED" / "DISCONNECTED" / ""
  //                 (empty == sysfs read failed; treated as "no signal")
  //   pending     - the outbox's queued byte count (0 if no client)
  //   client_open - true if the serial link is currently open
  //   now_ms      - monotonic ms (any reference epoch)
  Action tick(const std::string& usb_state, std::size_t pending, bool client_open,
              std::int64_t now_ms) {
    bool edge = !prev_usb_state_.empty() && prev_usb_state_ != "CONFIGURED" &&
                usb_state == "CONFIGURED";
    if (!usb_state.empty()) prev_usb_state_ = usb_state;

    // A drop in pending means a reader actually drained us — only THEN is a
    // later stall evidence of a stale fd (host closed its TTY). Without this gate
    // "nobody ever read" looks identical to "reader went away" and we'd reopen
    // (blocking gs_close) every few ticks for no reason.
    if (client_open && pending < last_pending_bytes_) had_reader_ = true;

    bool stalled = false;
    if (client_open && pending > 0 && pending >= last_pending_bytes_) {
      if (had_reader_ && ++no_drain_ticks_ >= kStallTicks &&
          usb_state == "CONFIGURED") {
        stalled = true;
      }
    } else {
      no_drain_ticks_ = 0;
    }
    last_pending_bytes_ = pending;

    bool retry = !client_open;
    if (!edge && !stalled && !retry) return Action::None;

    // Rate-limit. The retry path backs off exponentially after kBackoffStartAt
    // consecutive failures so a board where /dev/ttyGS0 never appears doesn't
    // flood. At consec == kBackoffStartAt the interval doubles, then doubles
    // each further failure.
    std::int64_t min_interval = kBaseRetryMs;
    if (retry && !edge && !stalled && consec_retry_failures_ >= kBackoffStartAt) {
      int shift = consec_retry_failures_ - kBackoffStartAt + 1;
      if (shift > 5) shift = 5;
      min_interval = kBaseRetryMs << shift;
      if (min_interval > kMaxRetryMs) min_interval = kMaxRetryMs;
    }
    if (now_ms - last_reopen_ms_ < min_interval) return Action::None;

    last_reopen_ms_ = now_ms;
    no_drain_ticks_ = 0;
    last_pending_bytes_ = 0;
    had_reader_ = false;  // fresh link — no reader observed yet
    return edge      ? Action::ReopenEdge
           : stalled ? Action::ReopenStalled
                     : Action::ReopenRetry;
  }

  // Caller invokes after acting on tick()'s recommendation, with whether the
  // reopen actually produced a working client. Drives the retry backoff.
  void on_reopen_result(bool succeeded) {
    if (succeeded) consec_retry_failures_ = 0;
    else ++consec_retry_failures_;
  }

  int consec_retry_failures() const { return consec_retry_failures_; }

 private:
  std::string prev_usb_state_;
  std::size_t last_pending_bytes_ = 0;
  bool had_reader_ = false;     // outbox observed draining since last (re)open
  int no_drain_ticks_ = 0;
  std::int64_t last_reopen_ms_ = std::numeric_limits<std::int64_t>::min() / 2;
  int consec_retry_failures_ = 0;
};

}  // namespace visio_schema::transport
