// The VREC container: an MCAP recording encrypted at rest.
//
// The property everything else rests on is that ciphertext offset equals
// plaintext offset plus the header — byte P of the plaintext is always
// keystream byte P — because that is what keeps MCAP's chunk index usable
// without decrypting from the start of the file. Most of this file is that
// one property, from several directions.
#include "visio_schema/mcap/recording_crypto.hpp"

#include <openssl/evp.h>

#include <gtest/gtest.h>

#include <algorithm>
#include <cstring>
#include <string>
#include <vector>

namespace visio_schema {
namespace mcap {
namespace {

RecordingKey TestKey(std::uint8_t seed) {
    RecordingKey k{};
    for (std::size_t i = 0; i < k.size(); ++i) {
        k[i] = static_cast<std::uint8_t>(i * 7 + seed);
    }
    return k;
}

RecordingNonce TestNonce() {
    RecordingNonce n{};
    for (std::size_t i = 0; i < n.size(); ++i) {
        n[i] = static_cast<std::uint8_t>(i + 1);
    }
    return n;
}

std::vector<std::uint8_t> Plaintext(std::size_t n) {
    std::vector<std::uint8_t> v(n);
    for (std::size_t i = 0; i < n; ++i) {
        v[i] = static_cast<std::uint8_t>(i * 31 + i / 97);
    }
    return v;
}

// The whole stream, encrypted in one call — the reference every random-access
// case below is compared against.
std::vector<std::uint8_t> Sequential(const std::vector<std::uint8_t>& plain) {
    RecordingCipher c(TestKey(0), TestNonce());
    std::vector<std::uint8_t> out(plain.size());
    EXPECT_TRUE(c.XorAt(0, plain.data(), plain.size(), out.data()));
    return out;
}

// EVP_chacha20 takes a 16-byte IV of counter(4, little-endian) || nonce(12),
// which is the seek primitive this container is built on. Pinned against
// RFC 8439 section 2.4.2 rather than assumed: if OpenSSL ever changed that
// layout, every existing recording would become unreadable and nothing else
// here would notice, because both sides would change together.
TEST(RecordingCrypto, ChaCha20IvLayoutMatchesRfc8439) {
    unsigned char key[32];
    for (int i = 0; i < 32; ++i) key[i] = static_cast<unsigned char>(i);
    const unsigned char nonce[12] = {0, 0, 0, 0, 0, 0, 0, 0x4a, 0, 0, 0, 0};
    unsigned char iv[16] = {1, 0, 0, 0};
    std::memcpy(iv + 4, nonce, sizeof(nonce));

    const char* pt =
        "Ladies and Gentlemen of the class of '99: If I could offer you only "
        "one tip for the future, sunscreen would be it.";
    const int len = static_cast<int>(std::strlen(pt));
    std::vector<unsigned char> ct(len);
    auto* ctx = EVP_CIPHER_CTX_new();
    ASSERT_NE(ctx, nullptr);
    int n = 0;
    ASSERT_EQ(EVP_EncryptInit_ex(ctx, EVP_chacha20(), nullptr, key, iv), 1);
    ASSERT_EQ(EVP_EncryptUpdate(ctx, ct.data(), &n,
                                reinterpret_cast<const unsigned char*>(pt),
                                len), 1);
    EVP_CIPHER_CTX_free(ctx);

    const unsigned char want[] = {0x6e, 0x2e, 0x35, 0x9a, 0x25, 0x68, 0xf9,
                                  0x80, 0x41, 0xba, 0x07, 0x28, 0xdd, 0x0d,
                                  0x69, 0x81};
    EXPECT_EQ(std::memcmp(ct.data(), want, sizeof(want)), 0);
}

TEST(RecordingCrypto, EncryptsAndIsItsOwnInverse) {
    const auto plain = Plaintext(300000);
    const auto cipher = Sequential(plain);
    ASSERT_EQ(cipher.size(), plain.size());
    EXPECT_NE(std::memcmp(cipher.data(), plain.data(), plain.size()), 0)
        << "the cipher produced the plaintext — it is a no-op";

    RecordingCipher c(TestKey(0), TestNonce());
    std::vector<std::uint8_t> back(plain.size());
    ASSERT_TRUE(c.XorAt(0, cipher.data(), cipher.size(), back.data()));
    EXPECT_EQ(back, plain);
}

// THE POINT OF THE CONTAINER. A reader seeks to an arbitrary plaintext offset
// and decrypts from there; unaligned offsets and 64-byte block boundaries are
// where a keystream-position bug hides.
TEST(RecordingCrypto, RandomAccessMatchesSequentialAtEveryOffset) {
    const auto plain = Plaintext(300000);
    const auto cipher = Sequential(plain);

    for (const std::size_t at : {std::size_t{0}, std::size_t{1},
                                 std::size_t{63}, std::size_t{64},
                                 std::size_t{65}, std::size_t{4095},
                                 std::size_t{4096}, std::size_t{100000},
                                 std::size_t{262144}, std::size_t{299999}}) {
        const std::size_t len = std::min<std::size_t>(1000, plain.size() - at);
        std::vector<std::uint8_t> piece(len);
        RecordingCipher c(TestKey(0), TestNonce());
        ASSERT_TRUE(c.XorAt(at, plain.data() + at, len, piece.data()));
        EXPECT_EQ(std::memcmp(piece.data(), cipher.data() + at, len), 0)
            << "offset " << at;
    }
}

// One cipher, seeking backwards then forwards — the reader's actual pattern
// when it walks a chunk index.
TEST(RecordingCrypto, SeeksBackwardsAndForwardsOnOneCipher) {
    const auto plain = Plaintext(300000);
    const auto cipher = Sequential(plain);
    RecordingCipher c(TestKey(0), TestNonce());

    std::vector<std::uint8_t> late(500), early(500);
    ASSERT_TRUE(c.XorAt(200000, plain.data() + 200000, 500, late.data()));
    ASSERT_TRUE(c.XorAt(50, plain.data() + 50, 500, early.data()));
    EXPECT_EQ(std::memcmp(late.data(), cipher.data() + 200000, 500), 0);
    EXPECT_EQ(std::memcmp(early.data(), cipher.data() + 50, 500), 0);
}

// The recorder writes in whatever sizes the MCAP writer flushes. Those must
// produce the same bytes as one monolithic call, or a file's decryptability
// would depend on how it happened to be chunked when written.
TEST(RecordingCrypto, ChunkedWritesMatchOneMonolithicWrite) {
    const auto plain = Plaintext(300000);
    const auto cipher = Sequential(plain);

    for (const std::size_t step : {std::size_t{1}, std::size_t{7},
                                   std::size_t{64}, std::size_t{4096},
                                   std::size_t{65536}}) {
        RecordingCipher c(TestKey(0), TestNonce());
        std::vector<std::uint8_t> out(plain.size());
        for (std::size_t at = 0; at < plain.size(); at += step) {
            const std::size_t len = std::min(step, plain.size() - at);
            ASSERT_TRUE(c.XorAt(at, plain.data() + at, len, out.data() + at));
        }
        EXPECT_EQ(out, cipher) << "step " << step;
    }
}

TEST(RecordingCrypto, InPlaceXorMatchesOutOfPlace) {
    const auto plain = Plaintext(5000);
    const auto cipher = Sequential(plain);
    std::vector<std::uint8_t> buf = plain;
    RecordingCipher c(TestKey(0), TestNonce());
    ASSERT_TRUE(c.XorAt(0, buf.data(), buf.size(), buf.data()));
    EXPECT_EQ(std::memcmp(buf.data(), cipher.data(), cipher.size()), 0);
}

TEST(RecordingCrypto, ADifferentKeyOrNonceProducesADifferentStream) {
    const auto plain = Plaintext(1000);
    const auto base = Sequential(plain);

    std::vector<std::uint8_t> other_key(1000), other_nonce(1000);
    RecordingCipher a(TestKey(1), TestNonce());
    ASSERT_TRUE(a.XorAt(0, plain.data(), plain.size(), other_key.data()));
    RecordingNonce n2 = TestNonce();
    n2[0] ^= 0xff;
    RecordingCipher b(TestKey(0), n2);
    ASSERT_TRUE(b.XorAt(0, plain.data(), plain.size(), other_nonce.data()));

    EXPECT_NE(other_key, base);
    EXPECT_NE(other_nonce, base) << "the nonce must reach the keystream";
}

TEST(RecordingCrypto, HeaderRoundTrips) {
    VrecHeader h;
    h.key_fp = RecordingKeyFingerprint(TestKey(0));
    h.nonce = TestNonce();

    std::uint8_t buf[kVrecHeaderBytes];
    WriteVrecHeader(h, buf);
    EXPECT_TRUE(LooksLikeVrec(buf, sizeof(buf)));

    VrecHeader got;
    std::string err;
    ASSERT_TRUE(ParseVrecHeader(buf, sizeof(buf), &got, &err)) << err;
    EXPECT_EQ(got.key_fp, h.key_fp);
    EXPECT_EQ(got.nonce, h.nonce);
    EXPECT_EQ(got.format, kVrecFormat);
    EXPECT_EQ(got.cipher, kVrecCipherChaCha20);
}

// A newer format must fail LOUDLY. Decrypting it with this cipher would
// produce plausible garbage that MCAP rejects somewhere deep inside, and the
// report would blame the recording rather than the reader.
TEST(RecordingCrypto, RejectsUnknownFormatsCiphersAndShortHeaders) {
    VrecHeader h;
    h.key_fp = RecordingKeyFingerprint(TestKey(0));
    h.nonce = TestNonce();
    std::uint8_t buf[kVrecHeaderBytes];
    WriteVrecHeader(h, buf);

    VrecHeader got;
    std::string err;
    EXPECT_FALSE(ParseVrecHeader(buf, kVrecHeaderBytes - 1, &got, &err));

    std::uint8_t future = buf[4];
    buf[4] = future + 1;
    EXPECT_FALSE(ParseVrecHeader(buf, sizeof(buf), &got, &err));
    EXPECT_NE(err.find("format"), std::string::npos) << err;
    buf[4] = future;

    buf[5] = kVrecCipherChaCha20 + 1;
    EXPECT_FALSE(ParseVrecHeader(buf, sizeof(buf), &got, &err));
    EXPECT_NE(err.find("cipher"), std::string::npos) << err;
    buf[5] = kVrecCipherChaCha20;

    buf[0] = 'X';
    EXPECT_FALSE(ParseVrecHeader(buf, sizeof(buf), &got, &err));
    EXPECT_FALSE(LooksLikeVrec(buf, sizeof(buf)));
}

// Plaintext MCAP starts with 0x89 'M' 'C' 'A' 'P'. A reader that accepts both
// sniffs these four bytes, so they must not collide.
TEST(RecordingCrypto, DoesNotMistakePlaintextMcapForAContainer) {
    const std::uint8_t mcap[] = {0x89, 'M', 'C', 'A', 'P', 0x30};
    EXPECT_FALSE(LooksLikeVrec(mcap, sizeof(mcap)));
    EXPECT_FALSE(LooksLikeVrec(mcap, 0));
}

// The SAME 8 bytes DeviceState.recording_key_fingerprint reports, so an admin
// comparing "what my device says" to "what opens this file" compares one
// string to itself. SHA-256(32 zero bytes)[:8].
TEST(RecordingCrypto, FingerprintIsTheDeviceReportedOne) {
    const RecordingKey zeros{};
    EXPECT_EQ(FingerprintHex(RecordingKeyFingerprint(zeros)),
              "66687aadf862bd77");
    EXPECT_EQ(FingerprintHex(RecordingKeyFingerprint(TestKey(0))).size(), 16u);
    EXPECT_NE(RecordingKeyFingerprint(TestKey(0)),
              RecordingKeyFingerprint(TestKey(1)));
}

TEST(RecordingCrypto, ZeroLengthWriteIsANoOp) {
    RecordingCipher c(TestKey(0), TestNonce());
    std::uint8_t byte = 0xab;
    EXPECT_TRUE(c.XorAt(0, &byte, 0, &byte));
    EXPECT_EQ(byte, 0xab);
}

} // namespace
} // namespace mcap
} // namespace visio_schema
