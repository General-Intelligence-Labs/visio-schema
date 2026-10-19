#include "visio_schema/wire/image.hpp"
#include "golden_vectors_test_util.hpp"
#include <gtest/gtest.h>

TEST(Image, ReadsPythonEnvelopeHeaders) {
    const auto fixture = visio_golden::Load("ota_image_vectors.txt");
    for (const char* name : {"raw", "gzip"}) {
        const auto& blob = fixture.at(name);
        visio_schema::wire::image::Header h;
        std::string error;
        ASSERT_TRUE(visio_schema::wire::image::ParseHeader(
            reinterpret_cast<const std::uint8_t*>(blob.data()), blob.size(), &h, &error)) << error;
        const auto encoded = visio_schema::wire::image::EncodeHeader(h);
        EXPECT_EQ(std::string(reinterpret_cast<const char*>(encoded.data()), encoded.size()),
                  blob.substr(0, encoded.size()));
        EXPECT_EQ(h.raw_bytes, fixture.at("plain").size());
        EXPECT_EQ(h.format, std::string(name) == "gzip" ? 3 : 1);
    }
}
