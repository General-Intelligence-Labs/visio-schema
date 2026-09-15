/*
 * recording_crypto.cc — see recording_crypto.hpp.
 *
 * OpenSSL EVP, so the RV1106's vendored 1.1.1h and a host 3.x agree. ChaCha20
 * there takes a 16-byte IV of counter(4, little-endian) || nonce(12), which is
 * exactly the seek primitive this needs: to read plaintext offset P, set the
 * counter to P / 64 and discard P % 64 bytes of keystream.
 */
#include "visio_schema/mcap/recording_crypto.hpp"

#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>
#include <openssl/sha.h>

#include <algorithm>
#include <cstring>

namespace visio_schema {
namespace mcap {
namespace {

// One ChaCha20 block of zeros, to skip the sub-block remainder after a seek.
// Static and const: shared, never written, no per-seek allocation.
const std::uint8_t kZeroBlock[kChaChaBlockBytes] = {0};

// Whether `suite`'s label fits the derivation buffer below.
bool LabelFits(const CipherSuite& suite) {
    return suite.key_label != nullptr &&
           std::strlen(suite.key_label) <= kMaxKeyLabelBytes;
}

// Domain separation: the per-file key is derived from the caller's key, so the
// caller's key itself never runs a cipher, one file's key tells you nothing
// about another's, and two suites over the SAME key derive into disjoint
// spaces. Callers must have checked LabelFits() first.
RecordingKey DeriveFileKey(const RecordingKey& key,
                           const RecordingNonce& nonce,
                           const CipherSuite& suite) {
    const std::size_t label_len = std::strlen(suite.key_label);
    std::uint8_t message[kMaxKeyLabelBytes + sizeof(RecordingNonce)];
    std::memcpy(message, suite.key_label, label_len);
    std::memcpy(message + label_len, nonce.data(), nonce.size());

    RecordingKey out{};
    unsigned int len = 0;
    HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()), message,
         label_len + nonce.size(), out.data(), &len);
    return out;
}

} // namespace

RecordingKeyFp RecordingKeyFingerprint(const RecordingKey& key) {
    std::uint8_t digest[SHA256_DIGEST_LENGTH];
    SHA256(key.data(), key.size(), digest);
    RecordingKeyFp fp{};
    std::memcpy(fp.data(), digest, fp.size());
    return fp;
}

std::string HexOf(const std::uint8_t* p, std::size_t n) {
    static const char* kDigits = "0123456789abcdef";
    std::string out;
    out.reserve(n * 2);
    for (std::size_t i = 0; i < n; ++i) {
        out.push_back(kDigits[p[i] >> 4]);
        out.push_back(kDigits[p[i] & 0x0f]);
    }
    return out;
}

std::string FingerprintHex(const RecordingKeyFp& fp) {
    return HexOf(fp.data(), fp.size());
}

void WriteVrecHeader(const VrecHeader& header, std::uint8_t* out,
                     const CipherSuite& suite) {
    std::memset(out, 0, kVrecHeaderBytes);
    std::memcpy(out, suite.magic.data(), suite.magic.size());
    out[4] = header.format;
    out[5] = header.cipher;
    std::memcpy(out + 8, header.key_fp.data(), header.key_fp.size());
    std::memcpy(out + 16, header.nonce.data(), header.nonce.size());
    // Little-endian by hand: the device is ARM LE and the readers are x86,
    // but a memcpy of a std::uint32_t would still be a promise about the host.
    const std::uint32_t v = header.bytes_valid;
    out[kVrecBytesValidOffset + 0] = static_cast<std::uint8_t>(v);
    out[kVrecBytesValidOffset + 1] = static_cast<std::uint8_t>(v >> 8);
    out[kVrecBytesValidOffset + 2] = static_cast<std::uint8_t>(v >> 16);
    out[kVrecBytesValidOffset + 3] = static_cast<std::uint8_t>(v >> 24);
}

bool LooksLikeVrec(const std::uint8_t* data, std::size_t len,
                   const CipherSuite& suite) {
    return len >= suite.magic.size() &&
           std::memcmp(data, suite.magic.data(), suite.magic.size()) == 0;
}

bool ParseVrecHeader(const std::uint8_t* data, std::size_t len,
                     VrecHeader* out, std::string* err,
                     const CipherSuite& suite) {
    auto fail = [&](const char* why) {
        if (err) *err = why;
        return false;
    };
    if (len < kVrecHeaderBytes) return fail("shorter than a container header");
    if (!LooksLikeVrec(data, len, suite)) return fail("wrong container magic");
    // A newer format must fail loudly. Decrypting it with this cipher would
    // produce plausible garbage that MCAP would then reject somewhere deep
    // inside, and the report would blame the recording rather than the reader.
    if (data[4] != kVrecFormat) return fail("unsupported container format version");
    if (data[5] != kVrecCipherChaCha20) return fail("unsupported container cipher");

    out->format = data[4];
    out->cipher = data[5];
    std::memcpy(out->key_fp.data(), data + 8, out->key_fp.size());
    std::memcpy(out->nonce.data(), data + 16, out->nonce.size());
    const std::uint8_t* b = data + kVrecBytesValidOffset;
    out->bytes_valid = static_cast<std::uint32_t>(b[0]) |
                       (static_cast<std::uint32_t>(b[1]) << 8) |
                       (static_cast<std::uint32_t>(b[2]) << 16) |
                       (static_cast<std::uint32_t>(b[3]) << 24);
    return true;
}

bool RandomNonce(RecordingNonce* out) {
    if (out == nullptr) return false;
    return RAND_bytes(out->data(), static_cast<int>(out->size())) == 1;
}

RecordingCipher::RecordingCipher(const RecordingKey& key,
                                 const RecordingNonce& nonce,
                                 const CipherSuite& suite)
    : nonce_(nonce) {
    // Refuse rather than derive from a truncated label: two suites whose
    // labels agreed after truncation would share a key space, which is the one
    // thing the label exists to prevent.
    if (!LabelFits(suite)) return;
    file_key_ = DeriveFileKey(key, nonce, suite);
    ctx_ = EVP_CIPHER_CTX_new();
    valid_ = ctx_ != nullptr;
}

RecordingCipher::~RecordingCipher() {
    if (ctx_) EVP_CIPHER_CTX_free(static_cast<EVP_CIPHER_CTX*>(ctx_));
    OPENSSL_cleanse(file_key_.data(), file_key_.size());
}

bool RecordingCipher::SeekTo(std::uint64_t at) {
    auto* ctx = static_cast<EVP_CIPHER_CTX*>(ctx_);
    const std::uint64_t block = at / kChaChaBlockBytes;
    const std::size_t within = static_cast<std::size_t>(at % kChaChaBlockBytes);

    std::uint8_t iv[16];
    for (int i = 0; i < 4; ++i) {
        iv[i] = static_cast<std::uint8_t>((block >> (8 * i)) & 0xff);
    }
    std::memcpy(iv + 4, nonce_.data(), nonce_.size());

    if (EVP_EncryptInit_ex(ctx, EVP_chacha20(), nullptr, file_key_.data(),
                           iv) != 1) {
        return false;
    }
    if (within > 0) {
        // Burn the part of the block that precedes `at`. Into a scratch on the
        // stack, never the caller's buffer.
        std::uint8_t scratch[kChaChaBlockBytes];
        int n = 0;
        if (EVP_EncryptUpdate(ctx, scratch, &n, kZeroBlock,
                              static_cast<int>(within)) != 1) {
            OPENSSL_cleanse(scratch, sizeof(scratch));
            return false;
        }
        OPENSSL_cleanse(scratch, sizeof(scratch));
    }
    position_ = at;
    positioned_ = true;
    return true;
}

bool RecordingCipher::XorAt(std::uint64_t at, const std::uint8_t* src,
                            std::size_t len, std::uint8_t* dst) {
    if (!valid_) return false;
    if (len == 0) return true;
    // Re-key only on a real seek. The recorder writes strictly forward, so
    // this is the branch that is never taken on the write path.
    if (!positioned_ || position_ != at) {
        if (!SeekTo(at)) return false;
    }
    auto* ctx = static_cast<EVP_CIPHER_CTX*>(ctx_);
    std::size_t done = 0;
    while (done < len) {
        // EVP takes int; chunk so a >2 GiB call cannot overflow it.
        const int chunk = static_cast<int>(
            std::min<std::size_t>(len - done, 1u << 30));
        int produced = 0;
        if (EVP_EncryptUpdate(ctx, dst + done, &produced, src + done,
                              chunk) != 1) {
            positioned_ = false;   // ctx state is now unknown; force a re-key
            return false;
        }
        done += static_cast<std::size_t>(produced);
        if (produced != chunk) {
            positioned_ = false;
            return false;
        }
    }
    position_ = at + len;
    return true;
}

} // namespace mcap
} // namespace visio_schema
