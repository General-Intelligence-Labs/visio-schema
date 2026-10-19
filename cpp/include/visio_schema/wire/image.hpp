#ifndef VISIO_SCHEMA_WIRE_IMAGE_HPP
#define VISIO_SCHEMA_WIRE_IMAGE_HPP

#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>

namespace visio_schema { namespace wire { namespace image {
constexpr std::size_t kHeaderBytes = 56;
constexpr std::uint8_t kRaw = 1;
constexpr std::uint8_t kGzip = 3;  // Format 2 is reserved for signed envelopes.
constexpr std::uint64_t kMaxImageBytes = 512ull << 20;

struct Header {
    std::uint8_t format = kRaw;
    std::array<std::uint8_t, 8> nonce{};
    std::uint64_t raw_bytes = 0;
    std::array<std::uint8_t, 32> sha256{};
};

inline std::array<std::uint8_t, kHeaderBytes> EncodeHeader(const Header& h) {
    std::array<std::uint8_t, kHeaderBytes> result{};
    std::memcpy(result.data(), "VENC", 4);
    result[4] = h.format;
    std::memcpy(result.data() + 8, h.nonce.data(), h.nonce.size());
    for (unsigned i = 0; i < 8; ++i)
        result[16+i] = static_cast<std::uint8_t>(h.raw_bytes >> (8*i));
    std::memcpy(result.data() + 24, h.sha256.data(), h.sha256.size());
    return result;
}

inline bool ParseHeader(const std::uint8_t* p, std::size_t n, Header* out,
                        std::string* error) {
    if (n < kHeaderBytes) { *error = "truncated VENC header"; return false; }
    if (std::memcmp(p, "VENC", 4) != 0) { *error = "bad VENC magic"; return false; }
    if (p[4] != kRaw && p[4] != kGzip) {
        *error = "unsupported VENC format"; return false;
    }
    if (p[4] == kGzip && (p[5] || p[6] || p[7])) {
        *error = "unsupported VENC compression flags"; return false;
    }
    out->format = p[4];
    std::memcpy(out->nonce.data(), p + 8, 8);
    out->raw_bytes = 0;
    for (int i = 0; i < 8; ++i)
        out->raw_bytes |= static_cast<std::uint64_t>(p[16+i]) << (8*i);
    if (out->format == kGzip && out->raw_bytes > kMaxImageBytes) {
        *error = "expanded image exceeds size limit"; return false;
    }
    std::memcpy(out->sha256.data(), p + 24, 32);
    return true;
}
}}}  // namespace visio_schema::wire::image
#endif
