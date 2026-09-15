/*
 * package.hpp - read a multi-board OTA package, streaming, on the device.
 *
 * The C++ half of python/visio_schema/wire/package.py. See that file for the
 * format and for WHY each rule is a rule; this header states only what the
 * device does with it.
 *
 * SHAPE. Deliberately the same three beats SlotStageSink already uses one level
 * down (ParsePrefix / PlanTargets / Route): buffer a bounded prefix, parse a
 * table out of it, and only then route byte ranges to destinations. Here the
 * destinations are boards rather than partitions, and the discipline it carries
 * up is the one that matters --
 *
 *     resolve and check EVERYTHING before the first erase: a refusal here
 *     keeps the running slot bootable, a refusal after an erase does not.
 *
 * The prefix needed is tiny (one 512-byte header plus the index, ~1.5 KiB), so
 * a package is planned long before any image byte has to go anywhere.
 *
 * NO JSON. The index is `key=value` lines, which is twenty lines to parse.
 * visio-schema's C++ has no libprotobuf, no abseil and no allocator games so it
 * cross-compiles for uClibc armv7; a JSON parser to read 300 bytes would be the
 * largest dependency in the library.
 */
#ifndef VISIO_SCHEMA_WIRE_PACKAGE_HPP
#define VISIO_SCHEMA_WIRE_PACKAGE_HPP

#include <openssl/hmac.h>

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace visio_schema {
namespace wire {
namespace package {

constexpr char kIndexName[] = "index.txt";
constexpr int kIndexVersion = 1;
constexpr std::size_t kBlock = 512;
// ustar puts its magic at offset 257 of the first header block -- the earliest
// point a reader can tell a package from a bare VENC envelope, and why a
// package is sniffed on a 512-byte prefix rather than four bytes.
constexpr std::size_t kUstarOffset = 257;
// A package whose index has not appeared by here is not one of ours. Bounded so
// a malformed stream cannot make the device buffer without limit.
constexpr std::size_t kMaxPrefixBytes = 64 * 1024;

enum class Parse { kNeedMore, kOk, kError };

struct Board {
    std::string role;
    std::string hwrev;      // == design_version == announced hardware_revision
    std::string equipment;
    std::string soc;
    std::string file;
    std::uint64_t bytes = 0;
    std::string sha256;     // hex, of the STORED VENC envelope
    // Where this board's image starts in the package stream. Derived from the
    // tar layout, then CHECKED against the real header when those bytes arrive
    // (see CheckMemberHeader) -- computing an offset and trusting it is how a
    // reader writes one board's image into another board's destination.
    std::uint64_t data_offset = 0;
};

struct Index {
    std::string product;
    std::string version;
    std::vector<Board> boards;   // APPLY order: leaves first, self last
};

inline bool IsPackage(const std::uint8_t* p, std::size_t n) {
    return n >= kUstarOffset + 5 &&
           std::memcmp(p + kUstarOffset, "ustar", 5) == 0;
}

namespace detail {

inline std::uint64_t RoundUp(std::uint64_t n) {
    return (n + kBlock - 1) / kBlock * kBlock;
}

// tar sizes are octal ASCII, NUL- or space-terminated.
inline bool Octal(const char* field, std::size_t len, std::uint64_t* out) {
    std::uint64_t v = 0;
    bool any = false;
    for (std::size_t i = 0; i < len; ++i) {
        const char c = field[i];
        if (c == '\0' || c == ' ') break;
        if (c < '0' || c > '7') return false;
        v = v * 8 + static_cast<std::uint64_t>(c - '0');
        any = true;
    }
    *out = v;
    return any;
}

inline std::string HeaderName(const std::uint8_t* h) {
    const char* p = reinterpret_cast<const char*>(h);
    const std::size_t n = ::strnlen(p, 100);
    return std::string(p, n);
}

inline bool HeaderSize(const std::uint8_t* h, std::uint64_t* out) {
    return Octal(reinterpret_cast<const char*>(h) + 124, 12, out);
}

inline std::string HmacHex(const std::string& body,
                           const std::uint8_t* key, std::size_t keylen) {
    unsigned char mac[32];
    unsigned int maclen = 0;
    HMAC(EVP_sha256(), key, static_cast<int>(keylen),
         reinterpret_cast<const unsigned char*>(body.data()), body.size(), mac,
         &maclen);
    static const char* x = "0123456789abcdef";
    std::string out;
    out.reserve(64);
    for (unsigned int i = 0; i < maclen; ++i) {
        out.push_back(x[(mac[i] >> 4) & 0xf]);
        out.push_back(x[mac[i] & 0xf]);
    }
    return out;
}

// Constant-time compare: the check is cheap and the failure loud, but a
// relabelled index is exactly what an attacker would iterate on.
inline bool SameDigest(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    unsigned char diff = 0;
    for (std::size_t i = 0; i < a.size(); ++i)
        diff |= static_cast<unsigned char>(a[i] ^ b[i]);
    return diff == 0;
}

inline bool SplitBoard(const std::string& v, Board* b) {
    std::string f[7];
    int n = 0;
    std::size_t start = 0;
    for (std::size_t i = 0; i <= v.size(); ++i) {
        if (i == v.size() || v[i] == ':') {
            if (n == 7) return false;   // too many fields
            f[n++] = v.substr(start, i - start);
            start = i + 1;
        }
    }
    if (n != 7) return false;
    b->role = f[0];
    b->hwrev = f[1];
    b->equipment = f[2];
    b->soc = f[3];
    b->file = f[4];
    b->sha256 = f[6];
    if (f[5].empty()) return false;
    char* end = nullptr;
    b->bytes = std::strtoull(f[5].c_str(), &end, 10);
    return end != nullptr && *end == '\0';
}

}  // namespace detail

// Parse the index out of `text`. Split from ParsePrefix so a host tool and the
// device agree on the grammar rather than each inventing one.
inline Parse ParseIndexText(const std::string& text, const std::uint8_t* key,
                            std::size_t keylen, Index* out, std::string* err) {
    std::string body, hmac_line;
    std::size_t pos = 0;
    std::vector<std::pair<std::string, std::string>> kv;
    while (pos <= text.size()) {
        const std::size_t nl = text.find('\n', pos);
        const std::string line =
            text.substr(pos, nl == std::string::npos ? std::string::npos
                                                     : nl - pos);
        if (nl == std::string::npos && line.empty()) break;
        if (line.rfind("hmac=", 0) == 0) {
            hmac_line = line.substr(5);
        } else {
            body += line;
            body += '\n';
            const std::size_t eq = line.find('=');
            if (eq != std::string::npos)
                kv.emplace_back(line.substr(0, eq), line.substr(eq + 1));
        }
        if (nl == std::string::npos) break;
        pos = nl + 1;
    }

    *out = Index();
    std::string version_field;
    for (const auto& p : kv) {
        if (p.first == "v") {
            version_field = p.second;
        } else if (p.first == "product") {
            out->product = p.second;
        } else if (p.first == "version") {
            out->version = p.second;
        } else if (p.first == "board") {
            Board b;
            if (!detail::SplitBoard(p.second, &b)) {
                *err = "malformed board line in " + std::string(kIndexName);
                return Parse::kError;
            }
            out->boards.push_back(b);
        }
    }

    if (version_field != std::to_string(kIndexVersion)) {
        *err = "package index version '" + version_field + "', expected " +
               std::to_string(kIndexVersion) + " - refusing to guess";
        return Parse::kError;
    }
    if (out->product.empty() || out->version.empty()) {
        *err = "package index is missing product or version";
        return Parse::kError;
    }
    if (out->boards.empty()) {
        *err = "the package declares no boards";
        return Parse::kError;
    }
    if (key != nullptr) {
        if (hmac_line.empty() ||
            !detail::SameDigest(hmac_line,
                                detail::HmacHex(body, key, keylen))) {
            *err = "package index HMAC does not verify - the board labels may "
                   "have been tampered with, and a mislabelled image "
                   "cross-flashes a limb";
            return Parse::kError;
        }
    }
    return Parse::kOk;
}

// Parse a package's index out of a stream prefix.
//
// kNeedMore sets `*need` to how many bytes from the START of the stream are
// required before trying again -- the same protocol ParseRkfwTable uses, so a
// caller that already buffers a prefix for the single-image case needs no new
// shape for this one.
inline Parse ParsePrefix(const std::uint8_t* p, std::size_t n,
                         const std::uint8_t* key, std::size_t keylen,
                         Index* out, std::size_t* need, std::string* err) {
    if (n < kBlock) {
        *need = kBlock;
        return Parse::kNeedMore;
    }
    if (!IsPackage(p, n)) {
        *err = "not a package (no ustar magic)";
        return Parse::kError;
    }
    const std::string name = detail::HeaderName(p);
    if (name != kIndexName) {
        // Tar has no table of contents, so without this ordering rule the
        // device could not validate the roster before it started writing.
        *err = "package member 0 is '" + name + "', not '" +
               std::string(kIndexName) + "'";
        return Parse::kError;
    }
    std::uint64_t index_size = 0;
    if (!detail::HeaderSize(p, &index_size)) {
        *err = "package index header has no readable size";
        return Parse::kError;
    }
    const std::uint64_t want = kBlock + detail::RoundUp(index_size);
    if (want > kMaxPrefixBytes) {
        *err = "package index larger than " + std::to_string(kMaxPrefixBytes) +
               " bytes - not one of ours";
        return Parse::kError;
    }
    if (n < want) {
        *need = static_cast<std::size_t>(want);
        return Parse::kNeedMore;
    }
    const std::string text(reinterpret_cast<const char*>(p) + kBlock,
                           static_cast<std::size_t>(index_size));
    if (ParseIndexText(text, key, keylen, out, err) != Parse::kOk)
        return Parse::kError;

    // Derive each image's offset from the tar layout. The index's board order
    // IS the file order, which the writer guarantees and CheckMemberHeader
    // verifies when the bytes actually arrive.
    std::uint64_t cursor = want;
    for (Board& b : out->boards) {
        b.data_offset = cursor + kBlock;
        cursor += kBlock + detail::RoundUp(b.bytes);
    }
    return Parse::kOk;
}

// Verify the 512-byte tar header that should sit immediately before `b`'s data.
// Computing an offset and trusting it is how a reader writes one board's image
// into another board's destination.
inline bool CheckMemberHeader(const std::uint8_t* header, const Board& b) {
    if (detail::HeaderName(header) != b.file) return false;
    std::uint64_t size = 0;
    return detail::HeaderSize(header, &size) && size == b.bytes;
}

// The total size a well-formed package should have, so a caller can refuse a
// truncated one at Begin rather than discovering it mid-route.
inline std::uint64_t ExpectedBytes(const Index& idx, std::uint64_t index_bytes) {
    std::uint64_t n = kBlock + detail::RoundUp(index_bytes);
    for (const Board& b : idx.boards) n += kBlock + detail::RoundUp(b.bytes);
    return n + 2 * kBlock;   // tar's two zero blocks of end-of-archive
}

}  // namespace package
}  // namespace wire
}  // namespace visio_schema

#endif  // VISIO_SCHEMA_WIRE_PACKAGE_HPP
