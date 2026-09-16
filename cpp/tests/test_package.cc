/*
 * test_package.cc - the C++ package reader against a package Python wrote.
 *
 * A format whose writer and reader are in different languages is exactly where
 * a field silently stops being read, so the fixture is a whole real package
 * (tests/golden/package_vectors.txt) rather than a description of one.
 */
#include "visio_schema/wire/package.hpp"

#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "golden_vectors_test_util.hpp"

namespace pkg = visio_schema::wire::package;

namespace {

struct Fixture {
    std::string bytes, key;
    std::map<std::string, std::string> v;
};

Fixture Load() {
    Fixture f;
    f.v = visio_golden::Load("package_vectors.txt");
    f.bytes = f.v.at("pkg");
    f.key = f.v.at("key");
    return f;
}

const std::uint8_t* U8(const std::string& s) {
    return reinterpret_cast<const std::uint8_t*>(s.data());
}

std::uint64_t BeU64(const std::string& s) {
    std::uint64_t v = 0;
    for (int i = 0; i < 8; ++i) v = (v << 8) | static_cast<unsigned char>(s[i]);
    return v;
}

pkg::Parse ParseAll(const Fixture& f, pkg::Index* idx, std::string* err) {
    std::size_t need = 0;
    return pkg::ParsePrefix(U8(f.bytes), f.bytes.size(), U8(f.key),
                            f.key.size(), idx, &need, err);
}

TEST(Package, ReadsWhatPythonWrote) {
    const Fixture f = Load();
    pkg::Index idx;
    std::string err;
    ASSERT_EQ(ParseAll(f, &idx, &err), pkg::Parse::kOk) << err;
    EXPECT_EQ(idx.product, f.v.at("product"));
    EXPECT_EQ(idx.version, f.v.at("version"));
    ASSERT_EQ(idx.boards.size(), 2u);
    for (std::size_t i = 0; i < idx.boards.size(); ++i) {
        char k[64];
        std::snprintf(k, sizeof(k), "board.%02zu.hwrev", i);
        EXPECT_EQ(idx.boards[i].hwrev, f.v.at(k));
        std::snprintf(k, sizeof(k), "board.%02zu.file", i);
        EXPECT_EQ(idx.boards[i].file, f.v.at(k));
        std::snprintf(k, sizeof(k), "board.%02zu.bytes", i);
        EXPECT_EQ(idx.boards[i].bytes, BeU64(f.v.at(k)));
    }
}

TEST(Package, TheDerivedOffsetsLandOnTheRealMemberHeaders) {
    // Computing an offset and trusting it is how a reader writes one board's
    // image into another board's destination.
    const Fixture f = Load();
    pkg::Index idx;
    std::string err;
    ASSERT_EQ(ParseAll(f, &idx, &err), pkg::Parse::kOk) << err;
    for (const pkg::Board& b : idx.boards) {
        ASSERT_GE(b.data_offset, pkg::kBlock);
        ASSERT_LE(b.data_offset + b.bytes, f.bytes.size());
        EXPECT_TRUE(pkg::CheckMemberHeader(
            U8(f.bytes) + b.data_offset - pkg::kBlock, b))
            << "derived offset for " << b.file << " is not its tar header";
        // and the payload at that offset really is that board's image
        const std::string data = f.bytes.substr(b.data_offset,
                                                static_cast<std::size_t>(b.bytes));
        EXPECT_EQ(data.substr(0, 4), std::string("\x07\x26\x45\x64", 4))
            << "image[i] = (i*31+7)&0xFF starts 07 26 45 64";
    }
}

TEST(Package, TheSelfImageIsLast) {
    const Fixture f = Load();
    pkg::Index idx;
    std::string err;
    ASSERT_EQ(ParseAll(f, &idx, &err), pkg::Parse::kOk) << err;
    EXPECT_EQ(idx.boards.back().hwrev, "ego_pro_head");
}

TEST(Package, NeedsMoreUntilTheIndexHasLanded) {
    // The same kNeedMore protocol ParseRkfwTable uses, so a caller that already
    // buffers a prefix for the single-image case needs no new shape.
    const Fixture f = Load();
    pkg::Index idx;
    std::string err;
    std::size_t need = 0;
    EXPECT_EQ(pkg::ParsePrefix(U8(f.bytes), 100, U8(f.key), f.key.size(), &idx,
                               &need, &err),
              pkg::Parse::kNeedMore);
    EXPECT_EQ(need, pkg::kBlock);
    EXPECT_EQ(pkg::ParsePrefix(U8(f.bytes), pkg::kBlock, U8(f.key),
                               f.key.size(), &idx, &need, &err),
              pkg::Parse::kNeedMore);
    EXPECT_GT(need, pkg::kBlock);
    // ...and the whole index fits well inside the bound.
    EXPECT_LT(need, pkg::kMaxPrefixBytes);
    EXPECT_EQ(pkg::ParsePrefix(U8(f.bytes), need, U8(f.key), f.key.size(), &idx,
                               &need, &err),
              pkg::Parse::kOk) << err;
}

TEST(Package, ARelabelledBoardFailsTheHmac) {
    // Every image is independently encrypted and self-digesting, so one cannot
    // be forged -- but swapping which BOARD an image claims to be for hands a
    // genuine head image to a limb whose own board mark then passes.
    const Fixture f = Load();
    std::string text =
        "v=1\nproduct=ego_pro\nversion=1.3.0\n"
        "board=r:compact_umi:gripper:rv1106:compact_umi.img:600:aa\n"
        "hmac=0000000000000000000000000000000000000000000000000000000000000000\n";
    pkg::Index idx;
    std::string err;
    EXPECT_EQ(pkg::ParseIndexText(text, U8(f.key), f.key.size(), &idx, &err),
              pkg::Parse::kError);
    EXPECT_NE(err.find("HMAC"), std::string::npos) << err;
    // ...and without a key it parses, so the refusal is the HMAC and not the
    // grammar.
    EXPECT_EQ(pkg::ParseIndexText(text, nullptr, 0, &idx, &err),
              pkg::Parse::kOk) << err;
}

TEST(Package, AnUnknownIndexVersionIsRefusedRatherThanGuessed) {
    pkg::Index idx;
    std::string err;
    EXPECT_EQ(pkg::ParseIndexText("v=2\nproduct=p\nversion=1.0.0\n"
                                  "board=r:h:e:s:f:1:aa\n",
                                  nullptr, 0, &idx, &err),
              pkg::Parse::kError);
    EXPECT_NE(err.find("refusing to guess"), std::string::npos) << err;
}

TEST(Package, AnIndexThatIsNotMemberZeroIsRefused) {
    const Fixture f = Load();
    std::string bad = f.bytes;
    // Rename member 0 -- tar has no table of contents, so without the ordering
    // rule the roster could not be validated before writing began.
    std::memcpy(&bad[0], "other.txt\0", 10);
    pkg::Index idx;
    std::string err;
    std::size_t need = 0;
    EXPECT_EQ(pkg::ParsePrefix(U8(bad), bad.size(), U8(f.key), f.key.size(),
                               &idx, &need, &err),
              pkg::Parse::kError);
    EXPECT_NE(err.find("member 0"), std::string::npos) << err;
}

TEST(Package, ABareVencIsNotAPackage) {
    // The device dispatches on exactly this: VENC is the single image every
    // fielded ego has always parsed.
    std::string venc = "VENC" + std::string(700, '\0');
    EXPECT_FALSE(pkg::IsPackage(U8(venc), venc.size()));
    const Fixture f = Load();
    EXPECT_TRUE(pkg::IsPackage(U8(f.bytes), f.bytes.size()));
}

}  // namespace
