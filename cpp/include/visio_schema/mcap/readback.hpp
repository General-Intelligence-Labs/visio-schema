// Write-time read-back: the options and the accounting for McapWriter's
// verification of what the storage medium holds. Opt-in (ring_bytes > 0).
//
// An SD card can hand back stale sectors for bytes the writer wrote
// correctly, and the only moment the correct bytes are still known is
// while they sit in RAM. So every byte the part file receives is also
// copied into a ring; once a writeback span (McapWriter's sync_span_bytes)
// is on the medium and evicted from the page cache it is queued, and
// ReadbackStep() reads it back from the medium (O_DIRECT) and compares. A
// mismatch is rewritten once from the ring; a span still wrong afterwards
// latches storage_fault() and recording continues.
//
// NO thread is created: the caller drives ReadbackStep() from a thread of
// its choosing, and a caller that never does simply accumulates
// spans_skipped — the writer is never blocked or slowed by the read-back.
// Linux only; elsewhere the options are ignored.
#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>

namespace visio_schema::mcap {

// Which read of a span a test seam is about to observe.
enum class McapReadbackPass { kFirstRead, kReadAfterRewrite };

struct McapReadbackOptions {
  // Recent output kept in RAM for comparison; 0 = read-back off. Rounded
  // up to a page multiple. It must hold every span still awaiting its
  // turn: two writeback spans, plus settle_bytes, plus what the writer
  // produces between two steps (write rate x stepping period), rounded up
  // to a MiB. A span the writer has overrun is skipped (counted, never
  // waited for). Note the span size depends on the container: a plaintext
  // part's writeback span is one MCAP chunk (~768 KiB), a VREC part's is
  // the cipher's 64 KiB slice.
  std::uint64_t ring_bytes = 0;
  // A span is read back only once this many further bytes have been
  // written past it, giving the medium time to settle it.
  std::uint64_t settle_bytes = 1 << 20;
  // One read per piece; a step's cost is bounded in pieces, not spans.
  std::size_t piece_bytes = 256 << 10;
  // Close() runs ReadbackStep inline for at most this long so the final
  // part's tail can be verified; 0 = Close never waits (tail skipped).
  int close_flush_ms = 0;
  // Test seam: invoked on the stepping thread right before a span is read
  // from the medium, so a test can alter the file in between. A production
  // caller leaves it empty.
  std::function<void(const std::string& path, std::uint64_t off,
                     std::uint64_t len, McapReadbackPass pass)>
      before_span_read_for_test;
};

// Device-log only by design: nothing here is exported beyond the struct.
struct McapReadbackStats {
  // On-medium bytes confirmed, on the first pass or after a repair.
  std::uint64_t spans_verified = 0;
  std::uint64_t bytes_verified = 0;
  // Failed the first pass...
  std::uint64_t spans_mismatched = 0;
  // ...of which these were repaired,
  std::uint64_t spans_rewritten_ok = 0;
  // and these were not (storage_fault() latches).
  std::uint64_t spans_unrepaired = 0;
  // Never verified for a non-storage reason: queue full, ring overrun,
  // writeback disabled for the part, still pending when Close() gave up.
  std::uint64_t spans_skipped = 0;
  // The read-back read itself came up short or failed (EIO on a span the
  // medium accepted is counted as unrepaired instead: a storage fault).
  std::uint64_t read_failed = 0;
  // How far the writer had run ahead of a span when it was picked up.
  std::uint64_t max_lag_bytes = 0;
};

}  // namespace visio_schema::mcap
