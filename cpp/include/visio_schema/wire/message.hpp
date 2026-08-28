// In-memory Visio wire message: the visio_schema.v1.wire.Header fields plus
// the opaque payload bytes.
//
// The C++ wire library is nanopb-only (no full libprotobuf) so it links on the
// device. Fields use the nanopb-generated enum + Timestamp types directly;
// `payload` is the inner message's serialized bytes, which this layer never
// parses. Higher layers (e.g. the Bus) own sequence stamping and the timesync
// `timestamp` rewrite.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include "google/protobuf/timestamp.pb.h"    // nanopb: google_protobuf_Timestamp
#include "visio_schema/v1/wire/header.pb.h"   // nanopb: Header + ControlStream

namespace visio_schema::wire {

// The payload bytes of a Message, shared and immutable.
//
// A message fans out to several sinks (framed legs, the MCAP recorder), and
// some of them must RETAIN it beyond the fanout call — as a plain string that
// retention was a full copy of every payload byte per retaining sink (~2 MB/s
// of memcpy on a busy camera device, on the dispatch thread). Payload makes
// the bytes exist ONCE: copying a Payload (and therefore a Message) is a
// refcount, and immutability is what makes the sharing sound.
//
// Ergonomics are string-shaped on purpose: construct from the std::string a
// producer just built (buffer moved in, never copied), read through `str()`
// or the implicit `const std::string&` conversion. An empty Payload reads as
// the empty string.
//
// Lifetime of the returned reference: it is the shared buffer itself, valid
// while ANY Payload (or adopted shared_ptr) still references it — but two
// string-member habits do not carry over: (a) binding `const std::string&`
// to a temporary's payload does not lifetime-extend through the conversion
// function; (b) reassigning the last Payload that references a buffer
// invalidates references previously taken from it.
class Payload {
 public:
  Payload() = default;
  // Producers hand over a built buffer; the string's storage moves, no copy.
  Payload(std::string bytes)
      : bytes_(std::make_shared<const std::string>(std::move(bytes))) {}
  // Copying ctor for byte ranges that do not outlive the call (an inbound
  // decode window).
  Payload(std::string_view bytes) : Payload(std::string(bytes)) {}
  // TEXT literals only (strlen-terminated — an embedded NUL truncates);
  // binary bytes go through the string/string_view constructors.
  Payload(const char* bytes) : Payload(std::string(bytes)) {}
  // Zero-copy adoption of an already-shared buffer (e.g. a cached announce
  // re-published every second). Explicit: sharing a buffer the producer may
  // still be able to reach is a deliberate act.
  explicit Payload(std::shared_ptr<const std::string> shared)
      : bytes_(std::move(shared)) {}

  const std::string& str() const {
    static const std::string kEmpty;
    return bytes_ ? *bytes_ : kEmpty;
  }
  operator const std::string&() const { return str(); }
  const char* data() const { return str().data(); }
  std::size_t size() const { return bytes_ ? bytes_->size() : 0; }
  bool empty() const { return size() == 0; }

  // Byte-wise equality. Exact-type overloads (not one string_view catch-all)
  // for two C++17 reasons: a `const char*` operand converts equally well to
  // string_view and to Payload, so a generic overload is ambiguous wherever
  // the implicit constructors are visible (gtest's EXPECT_EQ included); and
  // the conversion operator above makes std::string's own comparison
  // operators rival candidates, so only overloads exact on BOTH sides
  // resolve cleanly.
  friend bool operator==(const Payload& a, const Payload& b) {
    return a.str() == b.str();
  }
  friend bool operator==(const Payload& a, const std::string& b) {
    return a.str() == b;
  }
  friend bool operator==(const std::string& a, const Payload& b) {
    return b.str() == a;
  }
  friend bool operator==(const Payload& a, const char* b) {
    return a.str() == b;
  }
  friend bool operator==(const char* a, const Payload& b) {
    return b.str() == a;
  }
  friend bool operator!=(const Payload& a, const Payload& b) { return !(a == b); }
  friend bool operator!=(const Payload& a, const std::string& b) { return !(a == b); }
  friend bool operator!=(const std::string& a, const Payload& b) { return !(a == b); }
  friend bool operator!=(const Payload& a, const char* b) { return !(a == b); }
  friend bool operator!=(const char* a, const Payload& b) { return !(a == b); }

 private:
  std::shared_ptr<const std::string> bytes_;
};

// A stream is named globally by a topic string and labelled on the wire by a
// compact per-link `stream_id` (control ids < CONTROL_STREAM_FIRST_DYNAMIC are
// hop-local; data ids are negotiated and hub-remapped).
struct Message {
  std::uint32_t stream_id = 0;
  std::uint32_t seq = 0;
  google_protobuf_Timestamp timestamp = google_protobuf_Timestamp_init_zero;

  // The inner message's serialized bytes — shared, immutable, copied by
  // refcount (see Payload above).
  Payload payload;

  // In-memory only (NOT serialized into the wire Header): marks a high-bandwidth
  // bulk stream (camera video). A split-outbox endpoint sends non-bulk CONTROL
  // frames (command results, DeviceInfo, OTA status, IMU) on a separate queue
  // ahead of video, so a reply isn't stuck behind seconds of buffered H.265 on a
  // bandwidth-limited link. Set by the producer (publish_video).
  bool bulk = false;

  // SERIALIZED (Header.keyframe): this bulk frame is a SYNC POINT — an H.265
  // keyframe carrying VPS/SPS/PPS. A bounded outbox must never evict one, and a
  // recorder opens a video channel only on one. It crosses the wire because the
  // consumer is not always the producer — a hub relays video from its leaves.
  // Rationale and the scope of what that actually fixes: header.proto.
  bool keyframe = false;

  // In-memory only (NOT serialized): this message is SAFE TO SHED — a
  // periodic stream whose stale backlog is worthless to a recovering reader,
  // because the next message supersedes it (fused IMU quaternions, whose
  // ground truth ships full-rate in the raw bundles) or because it is only
  // meaningful live (audio playback; per-frame metadata paired with video
  // that is itself shed). A live sink drops these at the door on a STALLED
  // link, where framing them is pure waste — nothing is being delivered
  // anyway. One-shot events (ButtonEvent) and ground-truth bundles must NOT
  // set this: they queue through the stall and deliver on recovery.
  // Recording sinks ignore it; recordings stay lossless.
  //
  // Rate is NOT decided here — a client caps streams by topic
  // (transport/stream_policy.hpp).
  bool decimatable = false;

  // In-memory only (NOT serialized): some consumer may be RECORDING this
  // bulk stream at full rate right now, so a congested leg must NOT thin it
  // — the keyframes-only congestion tier (transport/framed_fd.hpp) passes
  // these frames untouched. Set by the producer per frame while that holds
  // (e.g. a device whose phone client leases the recording destination), so
  // there is no state to go stale: each frame carries its own truth. The
  // stall gate still applies — framing into a socket nobody reads helps no
  // recorder.
  bool no_degrade = false;

  // In-memory only (NOT serialized): cache of EncodeFramed(*this), filled by
  // the FIRST framed sink to send this message and reused by every other one.
  // Outbound framed bytes are byte-identical across sinks (the header is
  // stamped before fanout; per-link stream-id remap happens on hub INBOUND,
  // never per-sink), so one COBS+CRC pass serves the whole fanout. Safe
  // without locking: Bus::Relay hands the same Message to sinks sequentially
  // under its dispatch lock. `mutable` so Send(const Message&) can fill it.
  mutable std::shared_ptr<const std::vector<std::uint8_t>> framed;
};

}  // namespace visio_schema::wire
