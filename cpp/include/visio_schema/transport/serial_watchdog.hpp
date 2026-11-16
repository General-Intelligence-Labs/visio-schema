// SerialWatchdog — decides when /dev/ttyGS0 must be reopened. The CDC-ACM
// gadget
// gives no host-detached signal (DTR/TIOCMGET is gadget-driver-dependent), so
// we
// infer it from:
//   - a USB gadget-state edge into "CONFIGURED" (the host re-enumerated)
//   - the transport's progress-based stall state while USB looks healthy
//     (the host closed its TTY without dropping USB)
//   - persistent client-not-open: the initial open never succeeded (e.g.
//     /dev/ttyGS0 appears asynchronously after gadget bind) — periodic retry.
//
// I/O-free by design so unit tests drive it deterministically; the caller does
// the sysfs read, the reopen, and the logging. Ported from the device
// firmware's proven implementation.
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
    ReopenStalled,  // transport stalled with pending bytes while CONFIGURED
    ReopenRetry,    // no client open; periodic retry
  };

  // How many bytes must reach the fd, since THIS link opened, before we
  // believe a host is really reading. Not 1: a CDC-ACM gadget with nobody
  // attached still accepts writes into its own TX buffer (a few KB) before it
  // blocks, so "accepted > 0" is true on every fresh link whether or not a
  // host
  // exists. Comfortably above any gadget buffer, and a real reader on this
  // device passes it in well under a second (dual-eye video alone is ~2 MB/s).
  static constexpr std::uint64_t kReaderProofBytes = 256 * 1024;

  static constexpr std::int64_t kBaseRetryMs = 10000;    // 10 s
  static constexpr std::int64_t kMaxRetryMs = 300000;    // 5 min ceiling
  static constexpr int kBackoffStartAt = 3;  // back off after N failed retries

  // Inputs:
  //   usb_state   - "CONFIGURED" / "CONNECTED" / "DISCONNECTED" / ""
  //                 (empty == sysfs read failed; treated as "no signal")
  //   pending     - the outbox's queued byte count (0 if no client)
  //   accepted    - bytes the fd has accepted, EVER; monotonic across reopens.
  //                 The per-link proof threshold excludes gadget buffering.
  //   client_open - true if the serial link is currently open
  //   stalled     - the transport reports no progress with bytes pending
  //   now_ms      - monotonic ms (any reference epoch)
  Action tick(const std::string& usb_state, std::size_t pending,
              std::uint64_t accepted, bool client_open, bool stalled,
              std::int64_t now_ms) {
    bool edge = !prev_usb_state_.empty() && prev_usb_state_ != "CONFIGURED" &&
                usb_state == "CONFIGURED";
    if (!usb_state.empty()) prev_usb_state_ = usb_state;

    // Queue eviction is not proof of a reader. Only bytes accepted since this
    // link opened count, with enough traffic to exceed gadget TX buffering.
    if (client_open && accepted - accepted_at_open_ >= kReaderProofBytes)
      had_reader_ = true;

    // The transport measures time since actual write progress. Tick frequency
    // depends on I/O readiness and producer wakes, so it cannot measure
    // stalls.
    const bool drain_stalled = client_open && pending > 0 && had_reader_ &&
                               stalled && usb_state == "CONFIGURED";

    bool retry = !client_open;
    if (!edge && !drain_stalled && !retry) return Action::None;

    // Rate-limit. The retry path backs off exponentially after kBackoffStartAt
    // consecutive failures so a board where /dev/ttyGS0 never appears doesn't
    // flood. At consec == kBackoffStartAt the interval doubles, then doubles
    // each further failure.
    std::int64_t min_interval = kBaseRetryMs;
    if (retry && !edge && !drain_stalled &&
        consec_retry_failures_ >= kBackoffStartAt) {
      int shift = consec_retry_failures_ - kBackoffStartAt + 1;
      if (shift > 5) shift = 5;
      min_interval = kBaseRetryMs << shift;
      if (min_interval > kMaxRetryMs) min_interval = kMaxRetryMs;
    }
    if (now_ms - last_reopen_ms_ < min_interval) return Action::None;

    last_reopen_ms_ = now_ms;
    had_reader_ = false;  // fresh link — no reader observed yet
    // `accepted` is monotonic across reopens, so the proof threshold has to be
    // measured from THIS link's start or the previous link's traffic would
    // vouch for the new one.
    accepted_at_open_ = accepted;
    return edge            ? Action::ReopenEdge
           : drain_stalled ? Action::ReopenStalled
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
  std::uint64_t accepted_at_open_ = 0;
  bool had_reader_ = false;
  // the fd accepted bytes since the last (re)open
  std::int64_t last_reopen_ms_ = std::numeric_limits<std::int64_t>::min() / 2;
  int consec_retry_failures_ = 0;
};

}  // namespace visio_schema::transport
