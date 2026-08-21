/*
 * recording_crypto.hpp — the `VREC` container: an MCAP recording encrypted at
 * rest, on the card and in the bucket.
 *
 * WHY. The SD card is plaintext MCAP today and is additionally exported
 * read-only over USB-MTP on every customer image, so a cable is enough to copy
 * a whole shift. The operator holding the rig is the adversary. Under VREC the
 * card and the uploaded object are both ciphertext, and only the client admin
 * who minted the recording key can open them.
 *
 * FORMAT — 32-byte plaintext header, then the MCAP stream XORed with a
 * ChaCha20 keystream:
 *
 *    0  "VREC"   magic (4)
 *    4  fmt      u8  = 1
 *    5  cipher   u8  = 1 (chacha20)
 *    6  rsv      u16
 *    8  key_fp   (8)   SHA-256(recording_key)[:8] — which key opens this
 *   16  nonce    (12)  fresh per part
 *   28  rsv      (4)
 *   32  ciphertext -> EOF
 *
 *   file_key = HMAC-SHA256(recording_key, "visio-rec-v1" || nonce)
 *
 * THE OFFSET IDENTITY IS THE WHOLE DESIGN: ciphertext offset == plaintext
 * offset + 32. ChaCha20 is a stream cipher, so byte P of the plaintext is
 * always keystream byte P — a reader can seek to any offset and decrypt from
 * there without touching a byte before it. That is what keeps MCAP's chunk
 * index usable, and it is why this is NOT an AEAD.
 *
 * NO WHOLE-FILE TAG, AND THAT IS DELIBERATE. A Poly1305 over the stream can
 * only be written at close, so a part torn by a power cut — which is routine
 * here, the rig is switched off by hand — would become both unverifiable AND
 * unrecoverable, and mcap_repair.cpp exists precisely to recover those. The
 * trade is stated plainly: VREC gives confidentiality at rest, not
 * tamper-detection. Anyone who can write the card can corrupt a recording;
 * they still cannot read one.
 *
 * `key_fp` is the SAME 8 bytes DeviceState.recording_key_fingerprint reports,
 * so "what my device says" and "what opens this file" are one string.
 */
#ifndef VISIO_SCHEMA_MCAP_RECORDING_CRYPTO_HPP
#define VISIO_SCHEMA_MCAP_RECORDING_CRYPTO_HPP

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

namespace visio_schema {
namespace mcap {

using RecordingKey = std::array<std::uint8_t, 32>;
using RecordingNonce = std::array<std::uint8_t, 12>;
using RecordingKeyFp = std::array<std::uint8_t, 8>;

constexpr std::size_t kVrecHeaderBytes = 32;
constexpr std::uint8_t kVrecFormat = 1;
constexpr std::uint8_t kVrecCipherChaCha20 = 1;
// ChaCha20 block. A seek that is not block-aligned costs one discarded block.
constexpr std::size_t kChaChaBlockBytes = 64;

// SHA-256(key)[:8] — the fingerprint the header carries and the device reports.
RecordingKeyFp RecordingKeyFingerprint(const RecordingKey& key);
std::string FingerprintHex(const RecordingKeyFp& fp);

struct VrecHeader {
    std::uint8_t format = kVrecFormat;
    std::uint8_t cipher = kVrecCipherChaCha20;
    RecordingKeyFp key_fp{};
    RecordingNonce nonce{};
};

// Serialize into exactly kVrecHeaderBytes.
void WriteVrecHeader(const VrecHeader& header, std::uint8_t* out);

// Parse. False when `len` is short, the magic is wrong, or the format/cipher
// is one this build does not implement — a future format must fail loudly
// rather than be decrypted with the wrong cipher into plausible garbage.
bool ParseVrecHeader(const std::uint8_t* data, std::size_t len,
                     VrecHeader* out, std::string* err);

// True when `data` starts with the VREC magic. Cheap sniff for readers that
// accept both plaintext MCAP ("\x89MCAP") and VREC.
bool LooksLikeVrec(const std::uint8_t* data, std::size_t len);

// A fresh nonce for a new part, from the CSPRNG.
//
// False when the CSPRNG fails, and a caller that cannot get one MUST refuse to
// record rather than fall back to a fixed or counter nonce: two parts written
// under the same key and nonce share a keystream, and XORing the two
// ciphertexts together recovers both plaintexts without any key at all.
bool RandomNonce(RecordingNonce* out);

// Random-access ChaCha20 over one part.
//
// Holds no plaintext and no buffer: the caller supplies both sides, so this
// never allocates and can sit on the recorder's writer thread without adding
// an allocation to the write path.
class RecordingCipher {
public:
    // `key` is the client recording key; `nonce` comes from the part header.
    RecordingCipher(const RecordingKey& key, const RecordingNonce& nonce);
    ~RecordingCipher();
    RecordingCipher(const RecordingCipher&) = delete;
    RecordingCipher& operator=(const RecordingCipher&) = delete;

    bool valid() const { return valid_; }

    // XOR `len` bytes into `dst`, treating `src` as plaintext (or ciphertext —
    // the operation is its own inverse) starting at PLAINTEXT offset `at`.
    // `dst` may equal `src` for in-place work.
    //
    // Sequential calls are the fast path: a seek only costs a re-key when `at`
    // is not where the last call ended.
    bool XorAt(std::uint64_t at, const std::uint8_t* src, std::size_t len,
               std::uint8_t* dst);

private:
    bool SeekTo(std::uint64_t at);

    void* ctx_ = nullptr;          // EVP_CIPHER_CTX
    RecordingKey file_key_{};
    RecordingNonce nonce_{};
    std::uint64_t position_ = 0;   // plaintext offset the ctx is positioned at
    bool positioned_ = false;
    bool valid_ = false;
};

} // namespace mcap
} // namespace visio_schema

#endif // VISIO_SCHEMA_MCAP_RECORDING_CRYPTO_HPP
