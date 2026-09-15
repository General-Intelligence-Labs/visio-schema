/*
 * test_ota_vectors.cc - replay tests/golden/ota_vectors.txt through the C++ driver.
 *
 * The C++ end of the cross-language pin. python/tests/test_ota_vectors.py
 * replays the SAME file, so a rule that drifts between the two surfaces here as
 * a byte diff rather than on a board -- which is the whole reason the OTA client
 * stopped being reimplemented per language.
 */
#include "visio_schema/wire/ota.hpp"

#include <cstring>
#include <set>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "golden_vectors_test_util.hpp"

namespace ota = visio_schema::wire::ota;

namespace {

std::map<std::string, std::string> V;  // loaded once in main-ish fixture

std::string Key(const std::string& c, const char* suffix) { return c + suffix; }

std::string Step(const std::string& c, int n, const char* suffix) {
  char buf[64];
  std::snprintf(buf, sizeof(buf), ".%02d%s", n, suffix);
  return c + buf;
}

std::string Reply(const std::string& c, int n, int m) {
  char buf[64];
  std::snprintf(buf, sizeof(buf), ".%02d.reply.%02d", n, m);
  return c + buf;
}

std::uint64_t BeU64(const std::string& s, std::size_t off) {
  std::uint64_t v = 0;
  for (int i = 0; i < 8; ++i)
    v = (v << 8) | static_cast<unsigned char>(s[off + i]);
  return v;
}

std::uint32_t BeU32(const std::string& s, std::size_t off) {
  std::uint32_t v = 0;
  for (int i = 0; i < 4; ++i)
    v = (v << 8) | static_cast<unsigned char>(s[off + i]);
  return v;
}

// image[i] = (i * 31 + 7) & 0xFF -- a formula, so the vector carries no blob and
// an offset bug cannot hide behind a run of zeroes.
std::vector<std::uint8_t> Image(std::uint64_t n) {
  std::vector<std::uint8_t> out(static_cast<std::size_t>(n));
  for (std::uint64_t i = 0; i < n; ++i)
    out[static_cast<std::size_t>(i)] =
        static_cast<std::uint8_t>((i * 31 + 7) & 0xFF);
  return out;
}

std::set<std::string> CasesIn(const std::map<std::string, std::string>& v) {
  std::set<std::string> out;
  for (const auto& kv : v) out.insert(kv.first.substr(0, kv.first.find('.')));
  return out;
}

// The scripted peer: every send must match the transcript, and its replies then
// become available to recv().
struct Replay {
  std::string name;
  const std::map<std::string, std::string>* v;
  int n = 0;
  std::vector<std::string> outbox;
  double t = 0.0;
  bool mismatch = false;

  bool Send(const std::uint8_t* p, std::size_t len) {
    const std::string got(reinterpret_cast<const char*>(p), len);
    auto it = v->find(Step(name, n, ".send"));
    if (it == v->end()) {
      ADD_FAILURE() << name << ": sent " << (n + 1)
                    << " messages, the vector has " << n;
      mismatch = true;
      return false;
    }
    if (got != it->second) {
      ADD_FAILURE() << name << " step " << n
                    << ": emitted a different OtaMessage\n  want "
                    << visio_golden::Hex(it->second) << "\n  got  "
                    << visio_golden::Hex(got);
      mismatch = true;
      return false;
    }
    for (int m = 0;; ++m) {
      auto r = v->find(Reply(name, n, m));
      if (r == v->end()) break;
      outbox.push_back(r->second);
    }
    ++n;
    return true;
  }

  int Recv(double timeout, std::uint8_t* buf, std::size_t cap) {
    if (!outbox.empty()) {
      const std::string s = outbox.front();
      outbox.erase(outbox.begin());
      if (s.size() > cap) return -1;
      std::memcpy(buf, s.data(), s.size());
      return static_cast<int>(s.size());
    }
    if (timeout > 0) t += timeout;  // the driver is waiting; let time pass
    return -1;
  }
};

class OtaVectors : public ::testing::TestWithParam<std::string> {};

TEST_P(OtaVectors, TheDriverReproducesTheTranscript) {
  const std::string name = GetParam();
  const std::string params = V.at(Key(name, ".params"));
  ASSERT_EQ(params.size(), 32u) << "params is a fixed 32-byte struct";

  ota::Options opt;
  const std::uint64_t total = BeU64(params, 0);
  opt.chunk = BeU32(params, 8);
  opt.window = BeU32(params, 12);
  opt.chunk_cap = BeU32(params, 16);
  opt.session_id = BeU64(params, 20);
  opt.negotiate = params[29] != 0;
  opt.commit = params[30] != 0;
  opt.fw_version = V.at(Key(name, ".fw"));
  opt.board = V.at(Key(name, ".board"));
  opt.target_device = V.at(Key(name, ".target"));

  const std::vector<std::uint8_t> img = Image(total);
  Replay dev{name, &V};

  ota::Io io;
  io.image_bytes = total;
  io.send = [&dev](const std::uint8_t* p, std::size_t n) {
    return dev.Send(p, n);
  };
  io.recv = [&dev](double to, std::uint8_t* b, std::size_t c) {
    return dev.Recv(to, b, c);
  };
  io.clock = [&dev] { return dev.t; };
  io.read_image = [&img](std::uint64_t off, std::size_t n, std::uint8_t* out) {
    if (off + n > img.size()) return false;
    std::memcpy(out, img.data() + off, n);
    return true;
  };

  const ota::Outcome out = ota::Relay(io, opt);
  if (dev.mismatch) return;  // the diff above is the failure

  EXPECT_EQ(V.find(Step(name, dev.n, ".send")), V.end())
      << name << ": the vector expects more messages than the driver sent";

  const std::string want = V.at(Key(name, ".out"));
  ASSERT_EQ(want.size(), 22u);
  EXPECT_EQ(static_cast<int>(out.ok), static_cast<unsigned char>(want[0]));
  EXPECT_EQ(static_cast<int>(out.reason), static_cast<unsigned char>(want[1]))
      << "reason is the closed-set half the vectors pin";
  EXPECT_EQ(out.acked, BeU64(want, 2));
  EXPECT_EQ(out.total, BeU64(want, 10));
  EXPECT_EQ(out.resumes, BeU32(want, 18));
}

std::vector<std::string> LoadCases() {
  V = visio_golden::Load("ota_vectors.txt");
  const std::set<std::string> c = CasesIn(V);
  return std::vector<std::string>(c.begin(), c.end());
}

INSTANTIATE_TEST_SUITE_P(Golden, OtaVectors,
                         ::testing::ValuesIn(LoadCases()),
                         [](const testing::TestParamInfo<std::string>& i) {
                           return i.param;
                         });

TEST(OtaVectorsFile, IsActuallyLoaded) {
  // A vector file that fails to load makes every parameterised case vacuous.
  const auto v = visio_golden::Load("ota_vectors.txt");
  ASSERT_FALSE(v.empty()) << "no vectors under " << VISIO_GOLDEN_DIR;
  EXPECT_TRUE(v.count("happy_advert.params"));
}

}  // namespace
