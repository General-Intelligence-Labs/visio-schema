/*
 * test_wire_recordings.cc - the C++ recordings-pull client.
 *
 * Two halves: replay tests/golden/recordings_wire_vectors.txt (the same file the
 * Python and TypeScript suites replay, so a codec drift shows up as a byte diff),
 * and drive PullFile against a loopback sender that behaves like the firmware's
 * (one connection per open, bytes, close).
 */
#include "visio_schema/wire/recordings.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <cstring>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "golden_vectors_test_util.hpp"

namespace rec = visio_schema::wire::recordings;

namespace {

const char* kSession = "session_00042-1789017895";
const char* kCursor = "v1:1789017895000000:session_00042-1789017895";
constexpr std::uint64_t kMtime = 1789017895123456789ULL;

std::string Encoded(const rec::Command& cmd) {
    std::vector<std::uint8_t> out;
    EXPECT_TRUE(rec::Serialize(cmd, &out));
    return std::string(out.begin(), out.end());
}

rec::Result Decoded(const std::string& raw) {
    rec::Result r;
    EXPECT_TRUE(rec::DecodeResult(reinterpret_cast<const std::uint8_t*>(raw.data()), raw.size(), &r));
    return r;
}

// ---- vectors -------------------------------------------------------------------

TEST(RecordingsVectors, EveryCommandEncodesAsTheCorpusSays) {
    const auto v = visio_golden::Load("recordings_wire_vectors.txt");
    std::unique_ptr<rec::Command> cmd(new rec::Command);

    ASSERT_TRUE(rec::ListRecordingsCommand(cmd.get()));
    EXPECT_EQ(visio_golden::Hex(Encoded(*cmd)), visio_golden::Hex(v.at("list.cmd")));

    ASSERT_TRUE(rec::ListRecordingsCommand(cmd.get(), 20, kCursor));
    EXPECT_EQ(visio_golden::Hex(Encoded(*cmd)), visio_golden::Hex(v.at("list_page.cmd")));

    ASSERT_TRUE(rec::ListRecordingsCommand(cmd.get(), 0, "", kSession, "GILABS-A"));
    EXPECT_EQ(visio_golden::Hex(Encoded(*cmd)), visio_golden::Hex(v.at("list_session.cmd")));

    ASSERT_TRUE(rec::OpenRecordingFileCommand(cmd.get(), kSession, "ego_0003.mcap"));
    EXPECT_EQ(visio_golden::Hex(Encoded(*cmd)), visio_golden::Hex(v.at("open.cmd")));

    ASSERT_TRUE(rec::OpenRecordingFileCommand(cmd.get(), kSession, "ego_0003.mcap", 100663296,
                                              281018368, kMtime, "GILABS-A"));
    EXPECT_EQ(visio_golden::Hex(Encoded(*cmd)), visio_golden::Hex(v.at("open_resume.cmd")));

    ASSERT_TRUE(rec::DeleteRecordingCommand(cmd.get(), kSession, "GILABS-A"));
    EXPECT_EQ(visio_golden::Hex(Encoded(*cmd)), visio_golden::Hex(v.at("delete.cmd")));
}

TEST(RecordingsVectors, EveryResultDecodes) {
    const auto v = visio_golden::Load("recordings_wire_vectors.txt");

    const rec::Result open = Decoded(v.at("result_open.result"));
    EXPECT_EQ(open.command_id, 7u);
    EXPECT_TRUE(open.ok);
    ASSERT_EQ(open.payload, rec::Result::Payload::FileOpen);
    EXPECT_EQ(open.file_open.port, 50002u);
    EXPECT_EQ(open.file_open.offset, 100663296u);
    EXPECT_EQ(open.file_open.length, 180355072u);
    EXPECT_EQ(open.file_open.file_size, 281018368u);
    EXPECT_EQ(open.file_open.mtime_ns, kMtime);

    const rec::Result files = Decoded(v.at("result_session_files.result"));
    ASSERT_EQ(files.payload, rec::Result::Payload::Recordings);
    ASSERT_EQ(files.sessions.size(), 1u);
    const rec::SessionInfo& s = files.sessions[0];
    EXPECT_EQ(s.name, kSession);
    EXPECT_TRUE(s.active);
    EXPECT_EQ(s.file_count, 3u);
    EXPECT_DOUBLE_EQ(s.duration_s, 12.5);
    ASSERT_EQ(s.files.size(), 3u);
    EXPECT_EQ(s.files[0].name, "ego_0000.mcap");
    EXPECT_FALSE(s.files[0].writing);
    EXPECT_TRUE(s.files[0].complete);
    EXPECT_EQ(s.files[1].name, "ego_0001.mcap");
    EXPECT_TRUE(s.files[1].writing);
    EXPECT_FALSE(s.files[1].complete);
    EXPECT_EQ(s.files[2].name, "session.json");
    EXPECT_TRUE(s.files[2].writing);
    EXPECT_EQ(s.files[2].mtime_ns, kMtime + 2);

    const rec::Result page = Decoded(v.at("result_page.result"));
    ASSERT_EQ(page.payload, rec::Result::Payload::Recordings);
    ASSERT_EQ(page.sessions.size(), 2u);
    EXPECT_TRUE(page.sessions[1].damaged);
    EXPECT_EQ(page.next_cursor, kCursor);
    EXPECT_TRUE(page.read_while_recording);

    const rec::Result refused = Decoded(v.at("result_refused.result"));
    EXPECT_FALSE(refused.ok);
    EXPECT_EQ(refused.error_code, "writing");
    EXPECT_EQ(refused.error_message, "ego_0001.mcap is being recorded");
    EXPECT_EQ(refused.payload, rec::Result::Payload::None);
}

TEST(RecordingsBuilders, RefuseWhatTheDeviceCannotDecode) {
    std::unique_ptr<rec::Command> cmd(new rec::Command);
    EXPECT_FALSE(rec::OpenRecordingFileCommand(cmd.get(), "", "a.mcap"));
    EXPECT_FALSE(rec::OpenRecordingFileCommand(cmd.get(), ".hidden", "a.mcap"));
    EXPECT_FALSE(rec::OpenRecordingFileCommand(cmd.get(), "a/b", "a.mcap"));
    EXPECT_FALSE(rec::OpenRecordingFileCommand(cmd.get(), kSession, std::string(64, 'x')));
    EXPECT_TRUE(rec::OpenRecordingFileCommand(cmd.get(), std::string(63, 's'), std::string(63, 'f')));
    EXPECT_FALSE(rec::DeleteRecordingCommand(cmd.get(), kSession, ""));
    EXPECT_FALSE(rec::ListRecordingsCommand(cmd.get(), 0, std::string(64, 'c')));
}

// ---- pulling against a loopback sender ----------------------------------------------

class FakeDevice {
public:
    explicit FakeDevice(std::string data) : data_(std::move(data)) {
        listener_ = socket(AF_INET, SOCK_STREAM, 0);
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        bind(listener_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr));
        listen(listener_, 4);
        socklen_t len = sizeof(addr);
        getsockname(listener_, reinterpret_cast<sockaddr*>(&addr), &len);
        port_ = ntohs(addr.sin_port);
        thread_ = std::thread([this] { Serve(); });
    }
    ~FakeDevice() {
        stop_ = true;
        shutdown(listener_, SHUT_RDWR);
        close(listener_);
        thread_.join();
    }

    rec::Io Io(std::string* sink) {
        rec::Io io;
        io.run = [this](rec::Command* cmd, double, rec::Result* out) { return Run(*cmd, out); };
        io.sink = [sink](std::uint64_t offset, const std::uint8_t* d, std::size_t n) {
            EXPECT_EQ(offset, sink->size());
            sink->append(reinterpret_cast<const char*>(d), n);
            return true;
        };
        return io;
    }

    std::vector<std::uint64_t> cuts;  // bytes to send on the next connections
    bool refuse_sends = false;
    std::uint64_t mtime = kMtime;
    std::vector<std::uint64_t> open_offsets;
    std::vector<std::uint64_t> open_expect_mtimes;
    std::string data_;

private:
    bool Run(const rec::Command& cmd, rec::Result* out) {
        *out = rec::Result{};
        out->ok = true;
        if (cmd.which_body != visio_schema_v1_control_Command_open_recording_file_tag) {
            out->ok = false;
            out->error_code = "unsupported";
            return true;
        }
        const auto& b = cmd.body.open_recording_file;
        open_offsets.push_back(b.offset);
        open_expect_mtimes.push_back(b.expect_mtime_ns);
        if ((b.expect_size || b.expect_mtime_ns) &&
            (b.expect_size != data_.size() || b.expect_mtime_ns != mtime)) {
            out->ok = false;
            out->error_code = "changed";
            return true;
        }
        std::lock_guard<std::mutex> lock(mu_);
        std::uint64_t length = data_.size() - b.offset;
        pending_offset_ = b.offset;
        pending_length_ = length;
        pending_ = true;
        out->payload = rec::Result::Payload::FileOpen;
        out->file_open = rec::FileOpen{port_, b.offset, length, data_.size(), mtime};
        return true;
    }

    void Serve() {
        while (!stop_) {
            const int fd = accept(listener_, nullptr, nullptr);
            if (fd < 0) return;
            std::uint64_t off = 0, len = 0;
            bool have = false;
            {
                std::lock_guard<std::mutex> lock(mu_);
                have = pending_;
                pending_ = false;
                off = pending_offset_;
                len = pending_length_;
            }
            if (have && !refuse_sends) {
                if (!cuts.empty()) {
                    len = std::min(len, cuts.front());
                    cuts.erase(cuts.begin());
                }
                std::uint64_t sent = 0;
                while (sent < len) {
                    const ssize_t n = send(fd, data_.data() + off + sent, len - sent, MSG_NOSIGNAL);
                    if (n <= 0) break;
                    sent += static_cast<std::uint64_t>(n);
                }
                shutdown(fd, SHUT_WR);
            }
            close(fd);
        }
    }

    int listener_ = -1;
    std::uint16_t port_ = 0;
    std::thread thread_;
    std::atomic<bool> stop_{false};
    std::mutex mu_;
    bool pending_ = false;
    std::uint64_t pending_offset_ = 0;
    std::uint64_t pending_length_ = 0;
};

std::string Pattern(std::size_t n) {
    std::string s(n, '\0');
    for (std::size_t i = 0; i < n; ++i) s[i] = static_cast<char>((i * 31 + 7) & 0xFF);
    return s;
}

rec::PullRequest Request() {
    rec::PullRequest req;
    req.session_name = kSession;
    req.file_name = "ego_0000.mcap";
    return req;
}

TEST(RecordingsPull, WholeFileInOneOpen) {
    FakeDevice dev(Pattern(3 * 1024 * 1024 + 17));
    std::string got;
    const rec::Outcome o = rec::PullFile(dev.Io(&got), "127.0.0.1", Request());
    EXPECT_TRUE(o.ok) << o.code << " " << o.message;
    EXPECT_EQ(got, dev.data_);
    EXPECT_EQ(dev.open_offsets.size(), 1u);
    EXPECT_EQ(o.last_open.file_size, dev.data_.size());
}

TEST(RecordingsPull, EarlyCloseResumesAtTheBytesReceivedWithIdentityPinned) {
    FakeDevice dev(Pattern(2 * 1024 * 1024));
    dev.cuts = {100000, 300000};
    std::string got;
    const rec::Outcome o = rec::PullFile(dev.Io(&got), "127.0.0.1", Request());
    EXPECT_TRUE(o.ok) << o.code;
    EXPECT_EQ(got, dev.data_);
    EXPECT_EQ(dev.open_offsets, (std::vector<std::uint64_t>{0, 100000, 400000}));
    EXPECT_EQ(dev.open_expect_mtimes, (std::vector<std::uint64_t>{0, kMtime, kMtime}));
}

TEST(RecordingsPull, AFileThatChangesBetweenOpensFailsInsteadOfSplicing) {
    FakeDevice dev(Pattern(1024 * 1024));
    dev.cuts = {50000};
    std::string got;
    rec::Io io = dev.Io(&got);
    auto real_run = io.run;
    io.run = [&](rec::Command* cmd, double t, rec::Result* r) {
        if (!dev.open_offsets.empty()) dev.mtime += 1;
        return real_run(cmd, t, r);
    };
    const rec::Outcome o = rec::PullFile(io, "127.0.0.1", Request());
    EXPECT_FALSE(o.ok);
    EXPECT_EQ(o.code, "changed");
    EXPECT_EQ(got.size(), 50000u);
}

TEST(RecordingsPull, OpensThatDeliverNothingGiveUp) {
    FakeDevice dev(Pattern(4096));
    dev.refuse_sends = true;
    std::string got;
    rec::Options opt;
    opt.max_opens_without_progress = 3;
    const rec::Outcome o = rec::PullFile(dev.Io(&got), "127.0.0.1", Request(), opt);
    EXPECT_EQ(o.code, "no_progress");
    EXPECT_EQ(dev.open_offsets.size(), 3u);
}

TEST(RecordingsPull, AFailingSinkStopsThePull) {
    FakeDevice dev(Pattern(1024 * 1024));
    std::string got;
    rec::Io io = dev.Io(&got);
    io.sink = [](std::uint64_t, const std::uint8_t*, std::size_t) { return false; };
    const rec::Outcome o = rec::PullFile(io, "127.0.0.1", Request());
    EXPECT_EQ(o.code, "sink");
    EXPECT_EQ(dev.open_offsets.size(), 1u);
}

TEST(RecordingsPull, AnAlreadyCompleteFileNeedsNoSocket) {
    FakeDevice dev(Pattern(512));
    std::string got;
    rec::PullRequest req = Request();
    req.offset = 512;
    const rec::Outcome o = rec::PullFile(dev.Io(&got), "127.0.0.1", req);
    EXPECT_TRUE(o.ok);
    EXPECT_EQ(o.last_open.length, 0u);
}

TEST(RecordingsList, FollowsCursorsAndRefusesOneThatDoesNotAdvance) {
    int calls = 0;
    rec::Io io;
    io.run = [&](rec::Command* cmd, double, rec::Result* r) {
        *r = rec::Result{};
        r->ok = true;
        r->payload = rec::Result::Payload::Recordings;
        const std::string cursor = cmd->body.list_recordings.cursor;
        rec::SessionInfo s;
        s.name = "session_" + std::to_string(calls++);
        r->sessions.push_back(s);
        if (cursor.empty()) r->next_cursor = "p2";
        return true;
    };
    std::vector<rec::SessionInfo> out;
    std::string code;
    ASSERT_TRUE(rec::ListAllRecordings(io.run, "", &out, &code)) << code;
    EXPECT_EQ(out.size(), 2u);

    io.run = [&](rec::Command*, double, rec::Result* r) {
        *r = rec::Result{};
        r->ok = true;
        r->payload = rec::Result::Payload::Recordings;
        r->next_cursor = "same";
        return true;
    };
    EXPECT_FALSE(rec::ListAllRecordings(io.run, "", &out, &code));
    EXPECT_EQ(code, "protocol");
}

}  // namespace

TEST(RecordingsConstants, PullQuiesceRulesAreTheSpecList) {
  // Python PULL_QUIESCE_RULES and TS PULL_QUIESCE_RULES pin the same list.
  ASSERT_EQ(std::size(rec::kPullQuiesceRules), 4u);
  EXPECT_STREQ(rec::kPullQuiesceRules[0], "**/camera/*");
  EXPECT_STREQ(rec::kPullQuiesceRules[1], "**/imu/*/raw");
  EXPECT_STREQ(rec::kPullQuiesceRules[2], "**/imu/*/quat");
  EXPECT_STREQ(rec::kPullQuiesceRules[3], "**/audio/*");
}
