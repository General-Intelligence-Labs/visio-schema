// vendored_mcap — the header-only mcap library the writer's pieces share,
// configured here once so every one of them sees the same types. Private
// to src/mcap.
#pragma once

// Vendored header-only mcap, lz4/zstd compiled out (we only ever use
// Compression::None) so this links with no extra deps and cross-compiles for
// the device.
#define MCAP_COMPRESSION_NO_LZ4
#define MCAP_COMPRESSION_NO_ZSTD
#include <mcap/writer.hpp>
