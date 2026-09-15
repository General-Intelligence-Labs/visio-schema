/*
 * ota.hpp - drive a firmware OTA over the Visio bus. Owns no connection.
 *
 * The C++ half of python/visio_schema/wire/ota.py, mirrored name for name so the
 * cross-language conformance vectors read the same in both. Three
 * implementations of ONE spec, not one implementation in three syntaxes -- the
 * TypeScript one is async and this one blocks, and nobody should later "unify"
 * them into a template.
 *
 * WHO NEEDS THIS. Until now every OTA sender was a host tool; the device was
 * only ever the receiving half. A rig head has to become a real client -- it
 * unpacks one package and drives its own limbs -- and that is what this exists
 * for. It is also why the recursion the design allows is nearly free: a parent
 * speaks the same driver to a child that the app speaks to the parent.
 *
 * SHAPE. Every seam is a callback (Io), exactly as the Python injects
 * send/recv/clock/on_progress. Deliberately NOT a template: a template would put
 * the whole state machine in every translation unit that instantiates it, and
 * let the conformance test's fake diverge from the firmware's real one. One
 * indirect call per frame is nothing beside a 32 KiB NAND write.
 *
 * NO EXCEPTIONS ACROSS THE TRANSPORT SEAM. The Python maps OSError onto an
 * Outcome; firmware must not be required to throw. `send` returns false when the
 * link is gone and `recv` returns a negative length -- so a link drop is a
 * value, not a control-flow event.
 *
 * BLOCKING. Relay() holds its thread for the whole transfer. That is right on
 * the head's existing OTA worker and wrong on a dispatch thread; this codebase
 * has active objects, so someone will eventually try.
 *
 * gcc 8.3 / C++17: no <span>, no libprotobuf, no abseil -- it cross-compiles for
 * uClibc armv7.
 */
#ifndef VISIO_SCHEMA_WIRE_OTA_HPP
#define VISIO_SCHEMA_WIRE_OTA_HPP

#include <pb_decode.h>
#include <pb_encode.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

#include "visio_schema/v1/service/ota/ota.pb.h"

namespace visio_schema {
namespace wire {
namespace ota {

// ---- contract constants (mirror wire/ota.py) ------------------------------

// OtaChunk.data is a nanopb bytes array and nanopb is built without
// PB_FIELD_32BIT, so pb_size_t is 16-bit and PB_SIZE_MAX is 65535. A larger
// chunk cannot be pb_decode'd at all: the device answers every frame with
// "OtaMessage decode failed" and the transfer never advances, which reads like
// a dead link rather than a size problem. Headroom left for the wrapping fields.
constexpr std::uint32_t kMaxChunkBytes = 60 * 1024;
// Mirrors OtaManager::kMinAckIntervalBytes. Bounds what a DEVICE may talk us
// into, never what the caller asked for -- kUsbChunkBytes is deliberately below
// it, and flooring the caller's own choice would break the CDC-ACM leg.
constexpr std::uint32_t kMinChunkBytes = 8 * 1024;
constexpr std::uint32_t kTcpWindowBytes = 2 * 1024 * 1024;
constexpr std::uint32_t kTcpChunkBytes = 32 * 1024;
// USB CDC-ACM: total in-flight must stay within the gadget RX FIFO. The device
// decrypts and writes each chunk inline on its single I/O thread, so while it
// stalls in a flash write it stops reading, and a too-large window overruns the
// FIFO -> dropped bytes -> COBS CRC fail -> "gap in image stream".
constexpr std::uint32_t kUsbWindowBytes = 8 * 1024;
constexpr std::uint32_t kUsbChunkBytes = 4 * 1024;

constexpr std::uint64_t kDefaultSessionId = 0xCA11;
// The reserved session a head publishes a rig's whole-transaction verdict on.
// A relay folds only its OWN session, so a verdict never lands in a transfer.
// RIG, not bundle: "bundle" is the encrypted image throughout this stack, and
// the firmware spells this one kRigTerminalSession. The number is the contract.
constexpr std::uint64_t kRigTerminalSession = 0xB1D;

// What a pusher drops on its own link for the duration of a transfer. Why a
// link can only be quieted by the client that is ON it, and why that puts this
// in the driver rather than in every caller: docs/protocol/ota.md §6. Keep in
// step with the Python twin, visio_schema.wire.ota.QUIESCE_RULES (pinned by
// python/tests/test_wire_ota.py).
constexpr const char* kQuiesceRules[] = {"**/camera/*"};
constexpr std::size_t kQuiesceRuleCount =
    sizeof(kQuiesceRules) / sizeof(kQuiesceRules[0]);
constexpr std::uint32_t kQuiesceCommandId = 0xB15;

constexpr double kStallTimeoutS = 45.0;
constexpr double kSoftRetryS = 6.0;
constexpr double kCommitWaitS = 8.0;
constexpr double kDeadlineS = 900.0;
constexpr double kQueryWaitS = 1.5;

// Why a relay ended, as a CLOSED set. `detail` is prose for humans and free to
// be reworded; this is the half the conformance vectors pin, because three
// implementations cannot be held to a sentence. Values are wire-visible and
// APPEND-ONLY: never renumber, never reuse.
enum class Reason : std::uint8_t {
    kOkStaged = 0,
    kOkSuccess = 1,
    kOkStagedNoTransfer = 2,
    kOkCommittedUnconfirmed = 3,
    kOkLinkAfterCommit = 4,
    kFailDevice = 5,
    kFailStalled = 6,
    kFailDeadline = 7,
    kFailTooLossy = 8,
    kFailLinkDropped = 9,
    kOkUploadOnly = 13,
    // 10, 11 and 12 are RETIRED, not free. They belonged to the two-phase hold
    // a pusher no longer has. Append-only means never REUSE, not never remove.
};

struct Progress {
    std::uint64_t sent = 0;
    std::uint64_t acked = 0;
    std::uint64_t total = 0;
    std::uint32_t resumes = 0;
    const char* state = "";
};

struct Outcome {
    bool ok = false;
    Reason reason = Reason::kFailDevice;
    std::string detail;
    std::uint64_t acked = 0;
    std::uint64_t total = 0;
    std::uint32_t resumes = 0;
};

struct Io {
    // One serialized OtaMessage onto the device's OTA stream. false == the link
    // is gone; this is the ONLY way a transport reports that.
    std::function<bool(const std::uint8_t*, std::size_t)> send;
    // The next OtaStatus payload into `buf`, or <0 if none arrived within
    // `timeout_s`. A timeout of 0 must return immediately with whatever is
    // already buffered -- the send loop drains between frames, and blocking
    // there turns a windowed transfer into stop-and-wait.
    std::function<int(double, std::uint8_t*, std::size_t)> recv;
    std::function<double()> clock;
    std::function<void(const Progress&)> on_progress;  // optional
    // Random access to the image. A rig package is hundreds of MB; neither a
    // phone nor an RV1126B head can hold one in RAM.
    std::function<bool(std::uint64_t, std::size_t, std::uint8_t*)> read_image;
    // Drop kQuiesceRules on this link for the transfer, and put the link back
    // afterwards. Called with `true` before the begin and `false` on EVERY exit;
    // returns whether the device acked. Unset = push against a live link.
    // The transport supplies the mechanism; the driver owns the policy and
    // guarantees the restore. Contract: docs/protocol/ota.md §6.
    std::function<bool(bool)> quiesce;
    std::uint64_t image_bytes = 0;
};

struct Options {
    std::string fw_version;
    // The IMAGE's target board, never the device's: the unit compares it
    // against its own hardware_revision, so stamping it from what we read OFF
    // the device would make the check a tautology.
    std::string board;
    std::string target_device;
    std::uint64_t session_id = kDefaultSessionId;
    bool commit = true;        // false: upload every byte, then abort
    bool negotiate = true;
    std::uint32_t window = kTcpWindowBytes;
    std::uint32_t chunk = kTcpChunkBytes;
    std::uint32_t chunk_cap = 0;   // the LINK's limit; the device cannot see it
    std::uint32_t max_resumes = 0; // 0 = derive from the image size
    double stall_timeout_s = kStallTimeoutS;
    double soft_retry_s = kSoftRetryS;
    double commit_wait_s = kCommitWaitS;
    double deadline_s = kDeadlineS;
    double query_wait_s = kQueryWaitS;
};

// ---- encoders -------------------------------------------------------------

namespace detail {

using Message = visio_schema_v1_service_ota_OtaMessage;

inline void SetTarget(Message* m, const std::string& target,
                      std::uint64_t session_id) {
    // INITIALISE then copy, never `*m = <macro>`: the _init_zero macros are
    // braced initialiser lists, and assigning one is not valid C++ on the
    // cross-toolchain (gcc 8.3) even though a host compiler may allow it.
    const Message zero = visio_schema_v1_service_ota_OtaMessage_init_zero;
    *m = zero;
    m->session_id = session_id;
    // proto3 omits an empty string, so a relay that owns a dedicated link stays
    // byte-identical to what every fielded device has always been sent.
    std::snprintf(m->target_device, sizeof(m->target_device), "%s",
                  target.c_str());
}

inline bool Serialize(const Message& m, std::vector<std::uint8_t>* out) {
    pb_ostream_t sizer = PB_OSTREAM_SIZING;
    if (!pb_encode(&sizer, visio_schema_v1_service_ota_OtaMessage_fields, &m))
        return false;
    out->resize(sizer.bytes_written);
    pb_ostream_t os = pb_ostream_from_buffer(out->data(), out->size());
    return pb_encode(&os, visio_schema_v1_service_ota_OtaMessage_fields, &m);
}

}  // namespace detail

inline bool BeginMessage(const Options& o, std::uint64_t total,
                         std::uint32_t chunk, std::vector<std::uint8_t>* out) {
    detail::Message m;
    detail::SetTarget(&m, o.target_device, o.session_id);
    m.which_body = visio_schema_v1_service_ota_OtaMessage_begin_tag;
    m.body.begin.total_bytes = total;
    m.body.begin.chunk_bytes = chunk;
    std::snprintf(m.body.begin.fw_version, sizeof(m.body.begin.fw_version), "%s",
                  o.fw_version.c_str());
    std::snprintf(m.body.begin.board, sizeof(m.body.begin.board), "%s",
                  o.board.c_str());
    return detail::Serialize(m, out);
}

// `scratch` must hold PB_BYTES_ARRAY_T_ALLOCSIZE(n) bytes. Reused across chunks
// so a transfer does not malloc per frame.
inline bool ChunkMessage(const Options& o, std::uint64_t offset,
                         const std::uint8_t* data, std::size_t n,
                         std::vector<std::uint8_t>* scratch,
                         std::vector<std::uint8_t>* out) {
    scratch->resize(PB_BYTES_ARRAY_T_ALLOCSIZE(n));
    auto* arr = reinterpret_cast<pb_bytes_array_t*>(scratch->data());
    arr->size = static_cast<pb_size_t>(n);
    std::memcpy(arr->bytes, data, n);
    detail::Message m;
    detail::SetTarget(&m, o.target_device, o.session_id);
    m.which_body = visio_schema_v1_service_ota_OtaMessage_chunk_tag;
    m.body.chunk.offset = offset;
    m.body.chunk.data = arr;
    return detail::Serialize(m, out);
}

inline bool CommitMessage(const Options& o, std::vector<std::uint8_t>* out) {
    detail::Message m;
    detail::SetTarget(&m, o.target_device, o.session_id);
    m.which_body = visio_schema_v1_service_ota_OtaMessage_commit_tag;
    return detail::Serialize(m, out);
}

inline bool AbortMessage(const Options& o, const char* reason,
                         std::vector<std::uint8_t>* out) {
    detail::Message m;
    detail::SetTarget(&m, o.target_device, o.session_id);
    m.which_body = visio_schema_v1_service_ota_OtaMessage_abort_tag;
    std::snprintf(m.body.abort.reason, sizeof(m.body.abort.reason), "%s", reason);
    return detail::Serialize(m, out);
}

inline bool QueryMessage(const Options& o, std::vector<std::uint8_t>* out) {
    detail::Message m;
    detail::SetTarget(&m, o.target_device, o.session_id);
    m.which_body = visio_schema_v1_service_ota_OtaMessage_query_tag;
    return detail::Serialize(m, out);
}

inline bool ApplyMessage(const Options& o, std::vector<std::uint8_t>* out) {
    detail::Message m;
    detail::SetTarget(&m, o.target_device, o.session_id);
    m.which_body = visio_schema_v1_service_ota_OtaMessage_apply_tag;
    std::snprintf(m.body.apply.fw_version, sizeof(m.body.apply.fw_version), "%s",
                  o.fw_version.c_str());
    return detail::Serialize(m, out);
}

inline bool DecodeStatus(const std::uint8_t* raw, std::size_t n,
                         visio_schema_v1_service_ota_OtaStatus* out) {
    const visio_schema_v1_service_ota_OtaStatus zero =
        visio_schema_v1_service_ota_OtaStatus_init_zero;
    *out = zero;
    pb_istream_t is = pb_istream_from_buffer(raw, n);
    return pb_decode(&is, visio_schema_v1_service_ota_OtaStatus_fields, out);
}

// ---- the negotiation ------------------------------------------------------

// The chunk size to send, after asking the device what it can take. ota.proto
// requires this BEFORE the begin: chunk_bytes is the device's ack cadence as
// well as the frame size, so it cannot be renegotiated once a session is open.
//
// The four rules, each of which lived in exactly one implementation before:
//   * no advert (0) or no answer -> keep `want` UNTOUCHED (see kMinChunkBytes).
//   * an advert WINS over `want`, up or down; min(want, advert) would make the
//     field a no-op for every board that can take more than the default.
//   * SMALLEST advert across responders: a begin addressed to a board CLASS
//     reaches several units, and one image means one chunk size.
//   * `chunk_cap` applied LAST -- it is the LINK's limit, which the device
//     cannot see and so cannot report.
inline std::uint32_t NegotiateChunk(const Io& io, const Options& o) {
    std::vector<std::uint8_t> frame;
    if (!QueryMessage(o, &frame) || !io.send(frame.data(), frame.size()))
        return o.chunk_cap ? std::min(o.chunk, o.chunk_cap) : o.chunk;

    std::uint32_t smallest = 0;
    std::vector<std::uint8_t> buf(512);
    const double deadline = io.clock() + o.query_wait_s;
    while (io.clock() < deadline) {
        const double left = deadline - io.clock();
        const int n = io.recv(left > 0 ? left : 0.0, buf.data(), buf.size());
        if (n < 0) continue;
        visio_schema_v1_service_ota_OtaStatus s;
        if (!DecodeStatus(buf.data(), static_cast<std::size_t>(n), &s)) continue;
        if (s.session_id && s.session_id != o.session_id) continue;
        if (s.max_chunk_bytes)
            smallest = smallest == 0 ? s.max_chunk_bytes
                                     : std::min(smallest, s.max_chunk_bytes);
    }
    std::uint32_t out = o.chunk;
    if (smallest != 0)
        out = std::max(kMinChunkBytes, std::min(smallest, kMaxChunkBytes));
    return o.chunk_cap ? std::min(out, o.chunk_cap) : out;
}

// ---- the relay ------------------------------------------------------------

inline Outcome Relay(const Io& io, const Options& opt) {
    using OS = visio_schema_v1_service_ota_OtaStatus;
    const std::uint64_t total = io.image_bytes;

    struct {
        std::uint64_t acked = 0;
        bool staged = false;
        bool succeeded = false;
        bool has_failed = false;
        std::string failure;
        bool has_resume = false;
        std::uint64_t resume_to = 0;
    } st;

    std::uint32_t resumes = 0;
    bool committed = false;
    bool link_gone = false;
    std::vector<std::uint8_t> frame, scratch, payload, rxbuf(1024);

    auto done = [&](bool ok, const std::string& detail, Reason reason) {
        Outcome o;
        o.ok = ok;
        o.reason = reason;
        o.detail = detail;
        o.acked = st.acked;
        o.total = total;
        o.resumes = resumes;
        return o;
    };

    auto fold = [&](const std::uint8_t* raw, std::size_t n) {
        OS s;
        if (!DecodeStatus(raw, n, &s)) return;
        // A status stamped for some OTHER session is not ours to fold: its
        // bytes_received would move our watermark and its FAILED would abort a
        // healthy transfer. Unset (0) IS folded -- the device leaves it clear on
        // the periodic no-session IDLE, and older firmware never sets it.
        if (s.session_id && s.session_id != opt.session_id) return;
        if (s.state == visio_schema_v1_service_ota_OtaStatus_State_STAGED) {
            st.staged = true;
            if (s.bytes_received > st.acked) st.acked = s.bytes_received;
        } else if (s.state ==
                   visio_schema_v1_service_ota_OtaStatus_State_SUCCESS) {
            st.succeeded = true;
        } else if (s.state ==
                   visio_schema_v1_service_ota_OtaStatus_State_NEEDS_RESUME) {
            if (!st.staged && !st.succeeded) {
                st.has_resume = true;
                st.resume_to = s.bytes_received;
            }
        } else if (s.error_code[0] != '\0' ||
                   s.state == visio_schema_v1_service_ota_OtaStatus_State_FAILED) {
            // After staging the device has no session while it reboots, so our
            // last in-flight chunks bounce back as `no_session` FAILEDs --
            // ignore ONLY those; any other post-stage error is real.
            const bool bounce =
                (st.staged || st.succeeded) &&
                (s.error_code[0] == '\0' || std::strcmp(s.error_code,
                                                        "no_session") == 0);
            if (!bounce) {
                st.has_failed = true;
                st.failure = s.error_message[0] ? s.error_message
                             : (s.error_code[0] ? s.error_code
                                                : "device reported FAILED");
            }
        } else if (s.bytes_received > st.acked) {
            st.acked = s.bytes_received;
        }
    };

    // Fold every status available now, blocking up to `timeout` for the first.
    auto pump = [&](double timeout) {
        int n = io.recv(timeout, rxbuf.data(), rxbuf.size());
        while (n >= 0) {
            fold(rxbuf.data(), static_cast<std::size_t>(n));
            n = io.recv(0.0, rxbuf.data(), rxbuf.size());
        }
    };

    auto emit = [&](std::uint64_t sent, const char* state) {
        if (io.on_progress) {
            Progress p;
            p.sent = sent;
            p.acked = st.acked;
            p.total = total;
            p.resumes = resumes;
            p.state = state;
            io.on_progress(p);
        }
    };

    auto send_frame = [&](const std::vector<std::uint8_t>& f) {
        if (!io.send(f.data(), f.size())) {
            link_gone = true;
            return false;
        }
        return true;
    };

    auto abort_now = [&](const char* why) {  // best effort
        if (AbortMessage(opt, why, &frame)) io.send(frame.data(), frame.size());
    };

    // RAII, because Relay returns from a dozen places and the restore must
    // happen at every one of them.
    struct Quiet {
        const std::function<bool(bool)>* q;
        bool held = false;
        ~Quiet() {
            // `held` is only ever set from a hook that exists, so *q is live.
            // The catch is not optional: this runs during unwinding, where an
            // escaping exception is std::terminate.
            if (!held) return;
            try {
                (*q)(false);
            } catch (...) {
            }
        }
    } quiet{&io.quiesce};
    // Before the negotiation, not after: the OtaQuery's answer crosses the same
    // link the video is saturating, and a query that times out silently costs
    // the transfer its negotiated chunk size.
    if (io.quiesce) quiet.held = io.quiesce(true);


    std::uint32_t chunk = opt.chunk;
    if (opt.negotiate) {
        chunk = NegotiateChunk(io, opt);
    } else if (opt.chunk_cap) {
        chunk = std::min(chunk, opt.chunk_cap);
    }
    const std::uint32_t max_resumes =
        opt.max_resumes ? opt.max_resumes
                        : static_cast<std::uint32_t>(
                              2 * (total / (chunk ? chunk : 1) + 2) + 16);

    if (!BeginMessage(opt, total, chunk, &frame))
        return done(false, "could not encode OtaBegin", Reason::kFailDevice);
    if (!send_frame(frame))
        return done(false, "link dropped before the begin",
                    Reason::kFailLinkDropped);

    double t_start = io.clock();
    double last_progress = t_start, last_soft = t_start;
    emit(0, "begin");
    // Read once before streaming: a device whose other slot already holds this
    // build answers STAGED immediately, and a wrong-board refusal comes back
    // just as fast. Both are worth catching before pushing 60 MB.
    pump(0.0);

    std::uint64_t cursor = 0, last_ack = 0;
    payload.resize(chunk);

    while (!st.has_failed && !st.succeeded && !st.staged && !link_gone) {
        if (io.clock() - t_start > opt.deadline_s) {
            abort_now("deadline");
            return done(false, "exceeded the overall deadline",
                        Reason::kFailDeadline);
        }
        if (st.has_resume) {  // gap -> rewind + resend
            if (++resumes > max_resumes) {
                abort_now("too lossy");
                return done(false, "too lossy", Reason::kFailTooLossy);
            }
            cursor = st.resume_to;
            st.has_resume = false;  // NOTE: do NOT reset the stall clock
            continue;
        }
        const std::size_t n = static_cast<std::size_t>(
            std::min<std::uint64_t>(chunk, total - cursor));
        // Bound TOTAL in-flight (unacked) to `window`. With a non-blocking
        // transport this is the ONLY backpressure.
        if (cursor < total && (cursor - st.acked) + n <= opt.window) {
            if (!io.read_image(cursor, n, payload.data()))
                return done(false, "could not read the image",
                            Reason::kFailDevice);
            if (!ChunkMessage(opt, cursor, payload.data(), n, &scratch, &frame))
                return done(false, "could not encode OtaChunk",
                            Reason::kFailDevice);
            if (!send_frame(frame)) break;
            cursor += n;
            pump(0.0);
        } else if (cursor >= total && st.acked >= total) {
            break;  // all sent + acked -> commit
        } else {
            pump(0.05);
        }
        const double now = io.clock();
        if (st.acked > last_ack) {
            last_ack = st.acked;
            last_progress = last_soft = now;
        } else if (now - last_progress > opt.stall_timeout_s) {
            abort_now("stall");
            return done(false, "stalled", Reason::kFailStalled);
        } else if (now - last_soft > opt.soft_retry_s && !st.has_resume &&
                   cursor > st.acked) {
            last_soft = now;  // quiet tail -> re-drive from acked
            st.has_resume = true;
            st.resume_to = st.acked;
        }
    }

    if (link_gone)
        return done(false, "link dropped mid-transfer", Reason::kFailLinkDropped);
    if (st.has_failed) return done(false, st.failure, Reason::kFailDevice);
    if (st.staged)  // A/B instant revert: no transfer needed
        return done(true, "staged (no transfer needed)",
                    Reason::kOkStagedNoTransfer);

    if (!opt.commit) {
        // Everything arrived; deliberately do not flash it. Abort rather than
        // hanging up, so the device frees its staging now instead of holding a
        // half-session until the next push collides with it.
        abort_now("no-flash");
        return done(true, "uploaded, not committed", Reason::kOkUploadOnly);
    }

    emit(cursor, "committing");
    if (!CommitMessage(opt, &frame))
        return done(false, "could not encode OtaCommit", Reason::kFailDevice);
    if (!send_frame(frame))
        return done(false, "link dropped before the commit",
                    Reason::kFailLinkDropped);
    committed = true;
    (void)committed;

    const double deadline =
        io.clock() + std::min(opt.stall_timeout_s, opt.commit_wait_s);
    while (!st.staged && !st.succeeded && !st.has_failed &&
           io.clock() < deadline) {
        pump(0.05);
    }
    if (st.has_failed) return done(false, st.failure, Reason::kFailDevice);
    if (st.succeeded) return done(true, "SUCCESS", Reason::kOkSuccess);
    if (st.staged) return done(true, "STAGED", Reason::kOkStaged);
    // The device reboots to apply and its STAGED ack routinely races the link
    // drop, so DON'T require it -- the caller's post-reboot version check is
    // the real proof.
    return done(true, "committed (staging; STAGED ack raced the reboot)",
                Reason::kOkCommittedUnconfirmed);
}

}  // namespace ota
}  // namespace wire
}  // namespace visio_schema

#endif  // VISIO_SCHEMA_WIRE_OTA_HPP
