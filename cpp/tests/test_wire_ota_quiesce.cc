/*
 * test_wire_ota_quiesce.cc - the C++ driver's half of the quiesce contract.
 *
 * Why this file exists separately from test_ota_vectors.cc: the golden
 * transcript pins OtaMessage frames, and a quiesce is a Command on ANOTHER
 * stream -- it has no slot in the grammar and never will. So the vectors are
 * blind to this by construction, and the per-language unit suites ARE the pin
 * (docs/protocol/ota.md §6). Python has python/tests/test_wire_ota.py; without
 * this file the C++ driver -- the one a rig head runs against its limbs --
 * would pass every check with the hook never called and the restore never
 * firing.
 */
#include "visio_schema/wire/ota.hpp"

#include <cstring>
#include <string>
#include <vector>

#include <gtest/gtest.h>

namespace {

namespace ota = visio_schema::wire::ota;

constexpr std::uint64_t kTotal = 10000;
constexpr std::uint32_t kChunk = 1000;

// A limb that acks every chunk contiguously and STAGEs at commit -- the same
// shape OtaManager answers with.
class Peer {
public:
    bool Send(const std::uint8_t* p, std::size_t n) {
        if (dead_after_ >= 0 && sends_ >= dead_after_) return false;
        ++sends_;
        visio_schema_v1_service_ota_OtaMessage m =
            visio_schema_v1_service_ota_OtaMessage_init_zero;
        pb_istream_t is = pb_istream_from_buffer(p, n);
        if (!pb_decode(&is, visio_schema_v1_service_ota_OtaMessage_fields, &m))
            return true;
        if (m.which_body == visio_schema_v1_service_ota_OtaMessage_chunk_tag) {
            acked_ = m.body.chunk.offset + m.body.chunk.data->size;
            Reply(visio_schema_v1_service_ota_OtaStatus_State_RECEIVING);
        } else if (m.which_body ==
                   visio_schema_v1_service_ota_OtaMessage_commit_tag) {
            Reply(visio_schema_v1_service_ota_OtaStatus_State_STAGED);
        }
        pb_release(visio_schema_v1_service_ota_OtaMessage_fields, &m);
        return true;
    }

    int Recv(double, std::uint8_t* b, std::size_t cap) {
        if (outbox_.empty()) return -1;
        const std::string s = outbox_.front();
        outbox_.erase(outbox_.begin());
        if (s.size() > cap) return -1;
        std::memcpy(b, s.data(), s.size());
        return static_cast<int>(s.size());
    }

    void DieAfter(int sends) { dead_after_ = sends; }

private:
    void Reply(visio_schema_v1_service_ota_OtaStatus_State st) {
        visio_schema_v1_service_ota_OtaStatus s =
            visio_schema_v1_service_ota_OtaStatus_init_zero;
        s.state = st;
        s.bytes_received = acked_;
        std::string buf(64, '\0');
        pb_ostream_t os = pb_ostream_from_buffer(
            reinterpret_cast<pb_byte_t*>(buf.data()), buf.size());
        pb_encode(&os, visio_schema_v1_service_ota_OtaStatus_fields, &s);
        buf.resize(os.bytes_written);
        outbox_.push_back(std::move(buf));
    }

    std::vector<std::string> outbox_;
    std::uint64_t acked_ = 0;
    int sends_ = 0;
    int dead_after_ = -1;
};

struct Rig {
    Peer peer;
    std::vector<std::uint8_t> img = std::vector<std::uint8_t>(kTotal, 'x');
    std::vector<bool> calls;          // every quiesce(quiet) in order
    std::vector<std::string> order;   // "quiesce" / "query" / "begin"
    bool ack = true;
    double now = 0.0;

    ota::Io Io() {
        ota::Io io;
        io.image_bytes = kTotal;
        io.send = [this](const std::uint8_t* p, std::size_t n) {
            visio_schema_v1_service_ota_OtaMessage m =
                visio_schema_v1_service_ota_OtaMessage_init_zero;
            pb_istream_t is = pb_istream_from_buffer(p, n);
            if (pb_decode(&is, visio_schema_v1_service_ota_OtaMessage_fields,
                          &m)) {
                if (m.which_body ==
                    visio_schema_v1_service_ota_OtaMessage_query_tag)
                    order.push_back("query");
                else if (m.which_body ==
                         visio_schema_v1_service_ota_OtaMessage_begin_tag)
                    order.push_back("begin");
                pb_release(visio_schema_v1_service_ota_OtaMessage_fields, &m);
            }
            return peer.Send(p, n);
        };
        io.recv = [this](double t, std::uint8_t* b, std::size_t c) {
            // Virtual time advances only when the driver WAITS, so a 1.5 s
            // query wait costs nothing and a peer that never answers one still
            // terminates.
            now += 0.05;
            return peer.Recv(t, b, c);
        };
        io.clock = [this] { return now; };
        io.read_image = [this](std::uint64_t off, std::size_t n,
                               std::uint8_t* out) {
            if (off + n > img.size()) return false;
            std::memcpy(out, img.data() + off, n);
            return true;
        };
        io.quiesce = [this](bool quiet) {
            calls.push_back(quiet);
            order.push_back("quiesce");
            return ack;
        };
        return io;
    }

    ota::Options Opt(bool negotiate = false) {
        ota::Options o;
        o.fw_version = "1.0.0";
        o.board = "test_board";
        o.chunk = kChunk;
        o.negotiate = negotiate;
        return o;
    }
};

TEST(WireOtaQuiesce, QuietsBeforeTheBeginAndRestoresAtTheEnd) {
    Rig r;
    const ota::Outcome out = ota::Relay(r.Io(), r.Opt());
    ASSERT_TRUE(out.ok) << out.detail;
    EXPECT_EQ(r.calls, (std::vector<bool>{true, false}));
}

TEST(WireOtaQuiesce, RestoresEvenWhenTheTransferFails) {
    // The whole reason the guard is RAII: Relay returns from a dozen places,
    // and a link left dark is hardest to explain on the failures.
    Rig r;
    r.peer.DieAfter(2);
    const ota::Outcome out = ota::Relay(r.Io(), r.Opt());
    ASSERT_FALSE(out.ok);
    EXPECT_EQ(out.reason, ota::Reason::kFailLinkDropped);
    EXPECT_EQ(r.calls, (std::vector<bool>{true, false}));
}

TEST(WireOtaQuiesce, ADeviceThatNeverAckedIsNotRestored) {
    // Nothing was applied, so there is nothing to put back -- and a policy
    // REPLACES the link's previous one, so "undoing" what was never established
    // would clear whatever the device legitimately had.
    Rig r;
    r.ack = false;
    ota::Relay(r.Io(), r.Opt());
    EXPECT_EQ(r.calls, (std::vector<bool>{true}));
}

TEST(WireOtaQuiesce, TheQuiescePrecedesTheChunkNegotiation) {
    // The OtaQuery's answer crosses the same link the video is saturating, so
    // a query that times out under load silently costs the transfer its
    // negotiated chunk size.
    Rig r;
    ota::Relay(r.Io(), r.Opt(/*negotiate=*/true));
    ASSERT_GE(r.order.size(), 2u);
    EXPECT_EQ(r.order[0], "quiesce");
    EXPECT_EQ(r.order[1], "query");
}

TEST(WireOtaQuiesce, AnUnsetHookPushesAgainstALiveLink) {
    // `--no-pause-video` is a documented bench verb: measuring a push against a
    // loaded link on purpose must stay expressible, and must send NO policy.
    Rig r;
    ota::Io io = r.Io();
    io.quiesce = nullptr;
    const ota::Outcome out = ota::Relay(io, r.Opt());
    EXPECT_TRUE(out.ok) << out.detail;
    EXPECT_TRUE(r.calls.empty());
}

// The two reference drivers are held in step on quiesce by NOTHING else: the
// golden transcript cannot carry a Command. A 0xB15 that drifts produces the
// "device never acked" symptom on one language only.
TEST(WireOtaQuiesce, TheConstantsMatchThePythonTwin) {
    ASSERT_EQ(ota::kQuiesceRuleCount, 1u);
    EXPECT_STREQ(ota::kQuiesceRules[0], "**/camera/*");
    EXPECT_EQ(ota::kQuiesceCommandId, 0xB15u);
}

}  // namespace
