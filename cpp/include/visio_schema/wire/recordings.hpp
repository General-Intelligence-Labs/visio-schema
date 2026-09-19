/*
 * recordings.hpp - pull recorded sessions off a device: list, open, receive,
 * delete. Owns no bus connection.
 *
 * The C++ half of python/visio_schema/wire/recordings.py. Control rides the
 * existing Command / CommandResult pair (ListRecordings, OpenRecordingFile,
 * DeleteRecording). The file bytes do NOT ride the bus: an open names a TCP
 * port, and the host connects to it on the device address it sent the open to,
 * reads until the device closes, and never writes. Any direct IP link serves a
 * pull (USB-NCM or Wi-Fi). Exactly `length` bytes means the range arrived
 * whole; fewer means open again at the bytes received. TCP carries order,
 * delivery and flow control, so nothing here re-implements them. The contract
 * is docs/protocol/recordings_pull.md.
 *
 * SHAPE. Commands go through an injected `run` (the caller stamps command_id,
 * sends on COMMAND and decodes the matching CommandResult); `Receive` opens its
 * own POSIX socket because the byte path is a plain socket by design. No
 * exceptions: failures are values.
 *
 * BLOCKING. PullFile holds its thread for the whole file.
 *
 * gcc 8.3 / C++17, nanopb built with PB_ENABLE_MALLOC (the listing's names and
 * files are FT_POINTER).
 */
#ifndef VISIO_SCHEMA_WIRE_RECORDINGS_HPP
#define VISIO_SCHEMA_WIRE_RECORDINGS_HPP

#include <pb_decode.h>
#include <pb_encode.h>

#include <netdb.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include "visio_schema/v1/control/command.pb.h"
#include "visio_schema/v1/control/command_result.pb.h"

namespace visio_schema {
namespace wire {
namespace recordings {

// ---- contract constants (mirror wire/recordings.py) ------------------------

// Where current firmware's file sender listens (the open reports the port).
constexpr std::uint16_t kDefaultPort = 50002;
constexpr double kCommandTimeoutS = 8.0;
// No byte for this long on the data socket: the transfer is dead; resume.
constexpr double kStallTimeoutS = 10.0;
constexpr int kMaxOpensWithoutProgress = 5;
// What a host drops on its own bus link while it pulls (SetStreamPolicy rules,
// recordings_pull.md §7): video, IMU samples, audio. Restore on every exit.
constexpr const char* kPullQuiesceRules[] = {"**/camera/*", "**/imu/*/raw", "**/imu/*/quat",
                                             "**/audio/*"};
// nanopb.options max_size:64 for every name and the cursor. An oversized string
// fails the device's whole decode (it answers nothing), so it is refused here.
constexpr std::size_t kMaxNameBytes = 63;

using Command = visio_schema_v1_control_Command;
using CommandResult = visio_schema_v1_control_CommandResult;

inline bool PlainName(const std::string& n) {
    if (n.empty() || n.size() > kMaxNameBytes) return false;
    if (n[0] == '.') return false;  // also rejects "." and ".."
    return n.find('/') == std::string::npos && n.find('\0') == std::string::npos;
}

namespace detail {
inline bool CopyInto(char* dst, std::size_t cap, const std::string& s) {
    if (s.size() >= cap || s.find('\0') != std::string::npos) return false;
    std::memcpy(dst, s.data(), s.size());
    dst[s.size()] = '\0';
    return true;
}
}  // namespace detail

// ---- builders ----------------------------------------------------------------

// No cursor and no session = the original newest-`limit` call.
inline bool ListRecordingsCommand(Command* cmd, std::uint32_t limit = 0,
                                  const std::string& cursor = "",
                                  const std::string& session_name = "",
                                  const std::string& target_device = "") {
    std::memset(cmd, 0, sizeof(*cmd));
    if (!session_name.empty() && !PlainName(session_name)) return false;
    if (!detail::CopyInto(cmd->target_device, sizeof(cmd->target_device), target_device))
        return false;
    cmd->which_body = visio_schema_v1_control_Command_list_recordings_tag;
    auto& b = cmd->body.list_recordings;
    b.limit = limit;
    return detail::CopyInto(b.cursor, sizeof(b.cursor), cursor) &&
           detail::CopyInto(b.session_name, sizeof(b.session_name), session_name);
}

inline bool OpenRecordingFileCommand(Command* cmd, const std::string& session_name,
                                     const std::string& file_name, std::uint64_t offset = 0,
                                     std::uint64_t expect_size = 0,
                                     std::uint64_t expect_mtime_ns = 0,
                                     const std::string& target_device = "") {
    std::memset(cmd, 0, sizeof(*cmd));
    if (!PlainName(session_name) || !PlainName(file_name)) return false;
    if (!detail::CopyInto(cmd->target_device, sizeof(cmd->target_device), target_device))
        return false;
    cmd->which_body = visio_schema_v1_control_Command_open_recording_file_tag;
    auto& b = cmd->body.open_recording_file;
    b.offset = offset;
    b.expect_size = expect_size;
    b.expect_mtime_ns = expect_mtime_ns;
    return detail::CopyInto(b.session_name, sizeof(b.session_name), session_name) &&
           detail::CopyInto(b.file_name, sizeof(b.file_name), file_name);
}

// The device refuses a broadcast delete, so a target is required.
inline bool DeleteRecordingCommand(Command* cmd, const std::string& session_name,
                                   const std::string& target_device) {
    std::memset(cmd, 0, sizeof(*cmd));
    if (!PlainName(session_name) || target_device.empty()) return false;
    if (!detail::CopyInto(cmd->target_device, sizeof(cmd->target_device), target_device))
        return false;
    cmd->which_body = visio_schema_v1_control_Command_delete_recording_tag;
    return detail::CopyInto(cmd->body.delete_recording.session_name,
                            sizeof(cmd->body.delete_recording.session_name), session_name);
}

inline bool Serialize(const Command& cmd, std::vector<std::uint8_t>* out) {
    pb_ostream_t sizer = PB_OSTREAM_SIZING;
    if (!pb_encode(&sizer, visio_schema_v1_control_Command_fields, &cmd)) return false;
    out->resize(sizer.bytes_written);
    pb_ostream_t os = pb_ostream_from_buffer(out->data(), out->size());
    return pb_encode(&os, visio_schema_v1_control_Command_fields, &cmd);
}

// ---- results -----------------------------------------------------------------

struct FileInfo {
    std::string name;
    std::uint64_t size = 0;
    std::uint64_t mtime_ns = 0;
    bool writing = false;  // open for write: listed, not pullable
    bool complete = false;
    bool encrypted = false;
};

struct SessionInfo {
    std::string name;
    std::uint64_t size_bytes = 0;
    std::uint64_t started_at_us = 0;
    double duration_s = 0;
    bool active = false;
    bool damaged = false;
    std::uint32_t file_count = 0;
    std::vector<FileInfo> files;  // only for a session-scoped listing
};

struct FileOpen {
    std::uint32_t port = 0;
    std::uint64_t offset = 0;
    std::uint64_t length = 0;
    std::uint64_t file_size = 0;
    std::uint64_t mtime_ns = 0;
};

struct Result {
    enum class Payload { None, Recordings, FileOpen, Other };
    std::uint64_t command_id = 0;
    bool ok = false;
    std::string error_code;
    std::string error_message;
    Payload payload = Payload::None;
    std::vector<SessionInfo> sessions;
    std::string next_cursor;
    bool read_while_recording = false;
    FileOpen file_open;
};

inline bool DecodeResult(const std::uint8_t* data, std::size_t len, Result* out) {
    // Heap, not stack: CommandResult's union carries the whole DeviceState.
    std::unique_ptr<CommandResult> m(new CommandResult);
    std::memset(m.get(), 0, sizeof(*m));
    pb_istream_t is = pb_istream_from_buffer(data, len);
    const bool decoded = pb_decode(&is, visio_schema_v1_control_CommandResult_fields, m.get());
    if (decoded) {
        *out = Result{};
        out->command_id = m->command_id;
        out->ok = m->ok;
        out->error_code = m->error_code;
        out->error_message = m->error_message;
        switch (m->which_payload) {
            case 0:
                out->payload = Result::Payload::None;
                break;
            case visio_schema_v1_control_CommandResult_recordings_tag: {
                out->payload = Result::Payload::Recordings;
                const auto& list = m->payload.recordings;
                out->next_cursor = list.next_cursor ? list.next_cursor : "";
                out->read_while_recording = list.read_while_recording;
                for (pb_size_t i = 0; i < list.recordings_count; ++i) {
                    const auto& e = list.recordings[i];
                    SessionInfo s;
                    s.name = e.name ? e.name : "";
                    s.size_bytes = e.size_bytes;
                    s.started_at_us = e.started_at_us;
                    s.duration_s = e.duration_s;
                    s.active = e.active;
                    s.damaged = e.damaged;
                    s.file_count = e.file_count;
                    for (pb_size_t j = 0; j < e.files_count; ++j) {
                        const auto& f = e.files[j];
                        FileInfo fi;
                        fi.name = f.name ? f.name : "";
                        fi.size = f.size;
                        fi.mtime_ns = f.mtime_ns;
                        fi.writing = f.writing;
                        fi.complete = f.complete;
                        fi.encrypted = f.encrypted;
                        s.files.push_back(std::move(fi));
                    }
                    out->sessions.push_back(std::move(s));
                }
                break;
            }
            case visio_schema_v1_control_CommandResult_file_open_tag: {
                out->payload = Result::Payload::FileOpen;
                const auto& fo = m->payload.file_open;
                out->file_open = FileOpen{fo.port, fo.offset, fo.length, fo.file_size, fo.mtime_ns};
                break;
            }
            default:
                out->payload = Result::Payload::Other;
                break;
        }
    }
    pb_release(visio_schema_v1_control_CommandResult_fields, m.get());
    return decoded;
}

// ---- the byte path -------------------------------------------------------------

// Called in order with absolute offsets. Return false to stop (a failing sink,
// e.g. a full disk, ends the pull rather than looking like a dropped link).
using Sink = std::function<bool(std::uint64_t offset, const std::uint8_t* data, std::size_t len)>;

struct Received {
    std::uint64_t received = 0;
    int error = 0;             // errno when the socket stopped it short, else 0
    bool sink_failed = false;  // the sink returned false
};

// Read one opened range from `address`, the device address the open was sent
// to. A short read is not an error here: a refused connection, a reset, a stall
// and an early close all return what arrived.
// This reads only the data socket: keep the bus link read meanwhile, or the
// device aborts it after 10 s (recordings_pull.md §4).
inline Received Receive(const std::string& address, const FileOpen& open, const Sink& sink,
                        double stall_timeout_s = kStallTimeoutS) {
    Received r;
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    char port[8];
    std::snprintf(port, sizeof(port), "%u", static_cast<unsigned>(open.port));
    if (getaddrinfo(address.c_str(), port, &hints, &res) != 0 || res == nullptr) {
        r.error = EHOSTUNREACH;
        return r;
    }
    const int fd = socket(res->ai_family, res->ai_socktype | SOCK_CLOEXEC, res->ai_protocol);
    if (fd < 0) {
        r.error = errno;
        freeaddrinfo(res);
        return r;
    }
    timeval tv{};
    tv.tv_sec = static_cast<time_t>(stall_timeout_s);
    tv.tv_usec = static_cast<suseconds_t>((stall_timeout_s - static_cast<double>(tv.tv_sec)) * 1e6);
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));  // also bounds connect()
    const int rc = connect(fd, res->ai_addr, res->ai_addrlen);
    const int connect_errno = errno;
    freeaddrinfo(res);
    if (rc != 0) {
        r.error = connect_errno;
        close(fd);
        return r;
    }
    std::vector<std::uint8_t> buf(1 << 20);
    while (r.received < open.length) {
        const std::size_t want =
            static_cast<std::size_t>(std::min<std::uint64_t>(buf.size(), open.length - r.received));
        const ssize_t n = recv(fd, buf.data(), want, 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            r.error = errno;
            break;
        }
        if (n == 0) break;
        if (!sink(open.offset + r.received, buf.data(), static_cast<std::size_t>(n))) {
            r.sink_failed = true;
            break;
        }
        r.received += static_cast<std::uint64_t>(n);
    }
    close(fd);
    return r;
}

// ---- orchestration -------------------------------------------------------------

// Send `cmd` (stamp command_id first), wait up to timeout_s, decode the
// matching CommandResult into *result. false = no answer / link gone.
using RunCommand = std::function<bool(Command* cmd, double timeout_s, Result* result)>;

struct Io {
    RunCommand run;
    Sink sink;
    std::function<void(std::uint64_t offset, std::uint64_t size)> on_progress;  // optional
};

// One round trip plus the three answers every caller has to tell apart: no
// answer, a device refusal, and an answer of the wrong shape. Returns false
// with *code set; `want` is the payload the caller needs.
inline bool RunChecked(const RunCommand& run, Command* cmd, double timeout_s,
                       Result::Payload want, Result* result, std::string* code,
                       std::string* message = nullptr) {
    if (!run(cmd, timeout_s, result)) {
        *code = "no_answer";
        return false;
    }
    if (!result->ok) {
        *code = result->error_code.empty() ? "failed" : result->error_code;
        if (message) *message = result->error_message;
        return false;
    }
    if (result->payload != want) {
        *code = "protocol";
        if (message) *message = "the device answered with another payload";
        return false;
    }
    return true;
}

struct PullRequest {
    std::string session_name;
    std::string file_name;
    std::uint64_t offset = 0;
    std::uint64_t expect_size = 0;  // both 0 = unchecked
    std::uint64_t expect_mtime_ns = 0;
    std::string target_device;
};

struct Options {
    double command_timeout_s = kCommandTimeoutS;
    double stall_timeout_s = kStallTimeoutS;
    int max_opens_without_progress = kMaxOpensWithoutProgress;
};

struct Outcome {
    bool ok = false;
    // Empty on success. Otherwise the device's error_code, or one of
    // "invalid_request", "no_answer", "protocol", "sink", "no_progress".
    std::string code;
    std::string message;
    std::uint64_t offset = 0;  // bytes of the file now held by the sink
    FileOpen last_open;        // file_size + mtime_ns identify the file
};

// Pull one file from req.offset to its end. On a short read, open again at the
// bytes received with the size and mtime of the first open pinned, so a file
// that changes between attempts fails "changed" instead of being spliced.
inline Outcome PullFile(const Io& io, const std::string& address, PullRequest req,
                        const Options& opt = Options{}) {
    Outcome o;
    o.offset = req.offset;
    std::unique_ptr<Command> cmd(new Command);
    Result result;
    int idle_opens = 0;
    for (;;) {
        if (!OpenRecordingFileCommand(cmd.get(), req.session_name, req.file_name, o.offset,
                                      req.expect_size, req.expect_mtime_ns, req.target_device)) {
            o.code = "invalid_request";
            return o;
        }
        if (!RunChecked(io.run, cmd.get(), opt.command_timeout_s, Result::Payload::FileOpen,
                        &result, &o.code, &o.message)) {
            return o;
        }
        if (result.file_open.offset != o.offset ||
            result.file_open.offset + result.file_open.length != result.file_open.file_size) {
            o.code = "protocol";
            o.message = "OpenRecordingFile answer does not describe the requested range";
            return o;
        }
        const FileOpen fo = result.file_open;
        o.last_open = fo;
        req.expect_size = fo.file_size;
        req.expect_mtime_ns = fo.mtime_ns;
        if (fo.length == 0) {
            o.ok = true;
            return o;
        }
        const Received got = Receive(address, fo, io.sink, opt.stall_timeout_s);
        o.offset += got.received;
        if (io.on_progress) io.on_progress(o.offset, fo.file_size);
        if (got.sink_failed) {
            o.code = "sink";
            return o;
        }
        if (o.offset == fo.file_size) {
            o.ok = true;
            return o;
        }
        if (got.received != 0) {
            idle_opens = 0;
        } else if (++idle_opens >= opt.max_opens_without_progress) {
            o.code = "no_progress";
            o.message = std::string("opens delivered no bytes; last socket error: ") +
                        (got.error ? std::strerror(got.error) : "closed without data");
            return o;
        }
    }
}

// Every session, following cursors. Returns false with *code set on failure.
// Takes the runner, not an Io: a listing needs no byte sink, and requiring one
// to construct made callers invent an empty Sink for it.
inline bool ListAllRecordings(const RunCommand& run, const std::string& target_device,
                              std::vector<SessionInfo>* out, std::string* code,
                              double timeout_s = kCommandTimeoutS) {
    out->clear();
    std::unique_ptr<Command> cmd(new Command);
    Result result;
    std::string cursor;
    std::set<std::string> seen;
    for (;;) {
        if (!ListRecordingsCommand(cmd.get(), 0, cursor, "", target_device)) {
            *code = "invalid_request";
            return false;
        }
        if (!RunChecked(run, cmd.get(), timeout_s, Result::Payload::Recordings, &result, code))
            return false;
        for (auto& s : result.sessions) out->push_back(std::move(s));
        if (result.next_cursor.empty()) return true;
        if (!seen.insert(result.next_cursor).second) {
            *code = "protocol";  // the cursor did not advance
            return false;
        }
        cursor = result.next_cursor;
    }
}

}  // namespace recordings
}  // namespace wire
}  // namespace visio_schema

#endif  // VISIO_SCHEMA_WIRE_RECORDINGS_HPP
