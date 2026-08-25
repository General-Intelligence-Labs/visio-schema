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
    ReopenStalled,  // pending bytes stuck for >= kStallTicks while CONFIGURED
    ReopenRetry,    // no client open; periodic retry
  };

  // How many bytes must reach the fd, since THIS link opened, before we
  // believe a host is really reading. Not 1: a CDC-ACM gadget with nobody
  // attached still accepts writes into its own TX buffer (a few KB) before it
  // blocks, so "accepted > 0" is true on every fresh link whether or not a host
  // exists. Comfortably above any gadget buffer, and a real reader on this
  // device passes it in well under a second (dual-eye video alone is ~2 MB/s).
  static constexpr std::uint64_t kReaderProofBytes = 256 * 1024;

  static constexpr int kStallTicks = 3;            // ~6 s at 2 s ticks
  static constexpr std::int64_t kBaseRetryMs = 10000;    // 10 s
  static constexpr std::int64_t kMaxRetryMs = 300000;    // 5 min ceiling
  static constexpr int kBackoffStartAt = 3;        // back off after N failed retries

  // Inputs:
  //   usb_state   - "CONFIGURED" / "CONNECTED" / "DISCONNECTED" / ""
  //                 (empty == sysfs read failed; treated as "no signal")
  //   pending     - the outbox's queued byte count (0 if no client)
  //   accepted    - bytes the fd has accepted, EVER; monotonic across reopens.
  //                 Only a real reader advances it, which is why the
  //                 had_reader_ gate below keys off this and not `pending`.
  //   client_open - true if the serial link is currently open
  //   now_ms      - monotonic ms (any reference epoch)
  Action tick(const std::string& usb_state, std::size_t pending,
              std::uint64_t accepted, bool client_open, std::int64_t now_ms) {
    bool edge = !prev_usb_state_.empty() && prev_usb_state_ != "CONFIGURED" &&
                usb_state == "CONFIGURED";
    if (!usb_state.empty()) prev_usb_state_ = usb_state;

    // Only bytes the fd ACCEPTED prove a reader — only THEN is a later stall
    // evidence of a stale fd (host closed its TTY). Without this gate "nobody
    // ever read" looks identical to "reader went away" and we'd reopen
    // (blocking gs_close) every few ticks for no reason.
    //
    // This used to test `pending < last_pending_bytes_`, and a FALLING queue is
    // not a reader: a bounded outbox evicts its own backlog to stay bounded, so
    // pending drops on its own with nothing attached. That set had_reader_ on
    // the first eviction and re-armed the whole stall path. Measured on an ego
    // with nothing on /dev/ttyGS0: ReopenStalled every ~9 s for the life of the
    // process, and because each reopen clears the endpoint's stall latch,
    // full-rate dual-eye video refilled the outbox every cycle — 660,831 frames
    // door-dropped in one 40-minute boot.
    if (client_open && accepted - accepted_at_open_ >= kReaderProofBytes)
        had_reader_ = true;

    // Frozen `accepted` -- not a non-falling queue -- is what "the fd is taking
    // nothing" means. The gate above was fixed to stop reading a falling queue
    // as a reader; this test had the same flaw in mirror image, and leaving it
    // would have been the worse half: an outbox evicting itself reset
    // no_drain_ticks_ by luck of sampling, so a genuinely stale fd whose
    // backlog self-evicted more often than every kStallTicks would NEVER be
    // reopened. had_reader_ still gates it, so a link nobody ever read is quiet
    // rather than stalled.
    bool stalled = false;
    if (client_open && pending > 0 && accepted == last_accepted_) {
      if (had_reader_ && ++no_drain_ticks_ >= kStallTicks &&
          usb_state == "CONFIGURED") {
        stalled = true;
      }
    } else {
      no_drain_ticks_ = 0;
    }
    last_accepted_ = accepted;

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
    had_reader_ = false;  // fresh link — no reader observed yet
    // `accepted` is monotonic across reopens, so the proof threshold has to be
    // measured from THIS link's start or the previous link's traffic would
    // vouch for the new one.
    accepted_at_open_ = accepted;
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
  std::uint64_t last_accepted_ = 0;
  std::uint64_t accepted_at_open_ = 0;
  bool had_reader_ = false;     // the fd accepted bytes since the last (re)open
  int no_drain_ticks_ = 0;
  std::int64_t last_reopen_ms_ = std::numeric_limits<std::int64_t>::min() / 2;
  int consec_retry_failures_ = 0;
};

}  // namespace visio_schema::transport
