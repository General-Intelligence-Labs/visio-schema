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

constexpr char kMagic[4] = {'V', 'R', 'E', 'C'};
// Domain separation: the per-part file key is derived from the client key, so
// the client key itself never runs a cipher and one part's key tells you
// nothing about another's.
constexpr char kKeyLabel[] = "visio-rec-v1";

// One ChaCha20 block of zeros, to skip the sub-block remainder after a seek.
// Static and const: shared, never written, no per-seek allocation.
const std::uint8_t kZeroBlock[kChaChaBlockBytes] = {0};

RecordingKey DeriveFileKey(const RecordingKey& key,
                           const RecordingNonce& nonce) {
    std::uint8_t message[sizeof(kKeyLabel) - 1 + sizeof(RecordingNonce)];
    std::memcpy(message, kKeyLabel, sizeof(kKeyLabel) - 1);
    std::memcpy(message + sizeof(kKeyLabel) - 1, nonce.data(), nonce.size());

    RecordingKey out{};
    unsigned int len = 0;
    HMAC(EVP_sha256(), key.data(), static_cast<int>(key.size()), message,
         sizeof(message), out.data(), &len);
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

std::string FingerprintHex(const RecordingKeyFp& fp) {
    static const char* kDigits = "0123456789abcdef";
    std::string out;
    out.reserve(fp.size() * 2);
    for (const std::uint8_t b : fp) {
        out.push_back(kDigits[b >> 4]);
        out.push_back(kDigits[b & 0x0f]);
    }
    return out;
}

void WriteVrecHeader(const VrecHeader& header, std::uint8_t* out) {
    std::memset(out, 0, kVrecHeaderBytes);
    std::memcpy(out, kMagic, sizeof(kMagic));
    out[4] = header.format;
    out[5] = header.cipher;
    std::memcpy(out + 8, header.key_fp.data(), header.key_fp.size());
    std::memcpy(out + 16, header.nonce.data(), header.nonce.size());
}

bool LooksLikeVrec(const std::uint8_t* data, std::size_t len) {
    return len >= sizeof(kMagic) &&
           std::memcmp(data, kMagic, sizeof(kMagic)) == 0;
}

bool ParseVrecHeader(const std::uint8_t* data, std::size_t len,
                     VrecHeader* out, std::string* err) {
    auto fail = [&](const char* why) {
        if (err) *err = why;
        return false;
    };
    if (len < kVrecHeaderBytes) return fail("shorter than a VREC header");
    if (!LooksLikeVrec(data, len)) return fail("not a VREC container");
    // A newer format must fail loudly. Decrypting it with this cipher would
    // produce plausible garbage that MCAP would then reject somewhere deep
    // inside, and the report would blame the recording rather than the reader.
    if (data[4] != kVrecFormat) return fail("unsupported VREC format version");
    if (data[5] != kVrecCipherChaCha20) return fail("unsupported VREC cipher");

    out->format = data[4];
    out->cipher = data[5];
    std::memcpy(out->key_fp.data(), data + 8, out->key_fp.size());
    std::memcpy(out->nonce.data(), data + 16, out->nonce.size());
    return true;
}

bool RandomNonce(RecordingNonce* out) {
    if (out == nullptr) return false;
    return RAND_bytes(out->data(), static_cast<int>(out->size())) == 1;
}

RecordingCipher::RecordingCipher(const RecordingKey& key,
                                 const RecordingNonce& nonce)
    : nonce_(nonce) {
    file_key_ = DeriveFileKey(key, nonce);
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
