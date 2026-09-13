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

// A container's identity: the 4-byte magic it writes, and the HKDF-ish label
// that domain-separates its per-file key derivation.
//
// WHY THIS IS A PARAMETER AND NOT THREE COPIES OF THIS FILE. The diagnostic
// log wants exactly this construction — a 32-byte plaintext header, ChaCha20
// with the plaintext-offset identity, a per-file key derived from a fleet key
// — under a different magic so a reader cannot confuse the two, and a
// different label so the two key spaces cannot collide. Everything else is
// identical, and a forked copy would drift from the golden vectors that pin
// this one.
//
// `kVrecSuite` is the default on every entry point below, so every existing
// caller is untouched and the recording path cannot change by accident.
struct CipherSuite {
    std::array<char, 4> magic;
    const char* key_label;
};

// The recording container. Do not change: fielded cards are written with it.
inline constexpr CipherSuite kVrecSuite{{'V', 'R', 'E', 'C'}, "visio-rec-v1"};
// The diagnostic log's ring files on flash and on the SD card.
inline constexpr CipherSuite kVdlgSuite{{'V', 'D', 'L', 'G'}, "visio-diag-v1"};
// One diagnostic-log message on the bus. A SEPARATE label from kVdlgSuite even
// though both use the same fleet key: the file writer and the bus publisher
// are independent producers, and under one label they would be drawing from a
// single nonce space and would have to coordinate counters across two
// subsystems forever. Different labels make them different keystreams by
// construction.
inline constexpr CipherSuite kVdlwSuite{{'V', 'D', 'L', 'W'},
                                        "visio-diag-wire-v1"};

// Longest key label the derivation buffer holds. The suites above are the only
// callers and the longest is 18 bytes; this is a guard against a caller
// inventing one, not a real limit.
constexpr std::size_t kMaxKeyLabelBytes = 32;

// SHA-256(key)[:8] — the fingerprint the header carries and the device reports.
RecordingKeyFp RecordingKeyFingerprint(const RecordingKey& key);
std::string FingerprintHex(const RecordingKeyFp& fp);
// Lower-case hex of `n` bytes.
std::string HexOf(const std::uint8_t* p, std::size_t n);

struct VrecHeader {
    std::uint8_t format = kVrecFormat;
    std::uint8_t cipher = kVrecCipherChaCha20;
    RecordingKeyFp key_fp{};
    RecordingNonce nonce{};
    // Plaintext bytes the writer last recorded as valid, u32 LE at offset 28
    // (the bytes VREC has always left zero). 0 means "not recorded — read to
    // EOF", which keeps every VREC part torn-tail-readable exactly as before.
    // A `.vdlg` ring file rewrites this on each flush: without it, the bytes
    // past a power-cut tear in an appended file are keystream over whatever
    // was there, and a stream cipher will never flag them.
    std::uint32_t bytes_valid = 0;
};

// Offset of `bytes_valid` inside the header, for a writer that updates it in
// place after each flush without rewriting the rest.
constexpr std::size_t kVrecBytesValidOffset = 28;

// Serialize into exactly kVrecHeaderBytes, under `suite`'s magic.
void WriteVrecHeader(const VrecHeader& header, std::uint8_t* out,
                     const CipherSuite& suite = kVrecSuite);

// Parse. False when `len` is short, the magic is wrong, or the format/cipher
// is one this build does not implement — a future format must fail loudly
// rather than be decrypted with the wrong cipher into plausible garbage.
bool ParseVrecHeader(const std::uint8_t* data, std::size_t len,
                     VrecHeader* out, std::string* err,
                     const CipherSuite& suite = kVrecSuite);

// True when `data` starts with the VREC magic. Cheap sniff for readers that
// accept both plaintext MCAP ("\x89MCAP") and VREC.
bool LooksLikeVrec(const std::uint8_t* data, std::size_t len,
                   const CipherSuite& suite = kVrecSuite);

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
    // `suite` selects the key label. An over-long label leaves the cipher
    // invalid() rather than silently deriving from a truncated one.
    RecordingCipher(const RecordingKey& key, const RecordingNonce& nonce,
                    const CipherSuite& suite = kVrecSuite);
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
