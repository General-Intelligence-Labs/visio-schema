# Vendored mcap (C++)

Header-only MCAP C++ library from https://github.com/foxglove/mcap, pinned to
tag **`releases/cpp/v1.4.1`** (commit `0ebaf69`).

Only `cpp/mcap/include/` + `LICENSE` are vendored — the writer/reader are
header-only. The `visio` C++ library compiles `src/endpoints/mcap.cc` with
`-DMCAP_COMPRESSION_NO_LZ4 -DMCAP_COMPRESSION_NO_ZSTD` and only ever uses
`Compression::None`, so no lz4/zstd libraries are needed and the recording
endpoint cross-compiles for the RV1106 (vendor gcc 8.3 / uClibc) with no extra
dependencies.

## Why vendored sources rather than a git submodule
This workspace's environment cannot `git clone` GitHub over HTTPS for a
submodule init (only the smart-HTTP `ls-remote` / codeload tarball paths work),
so a submodule would not initialize on checkout. The headers are committed
directly instead.

## Why v1.4.1 and not the latest (v1.5.x)
The v1.5.x `writer.inl` adds an **unconditional** `#include <lz4.h>` that is not
guarded by `MCAP_COMPRESSION_NO_LZ4`, so the compression-disabled embedded build
fails to find the header. v1.4.1 guards every lz4/zstd include, which is what the
nanopb-only RV1106 build needs.

## Updating
Download the desired `releases/cpp/vX.Y.Z` tarball from
`https://codeload.github.com/foxglove/mcap/tar.gz/refs/tags/releases/cpp/vX.Y.Z`,
extract `cpp/mcap/include/` + `LICENSE` over this directory, and re-verify the
embedded cross-compile (no lz4/zstd symbols in `libvisio.a`).
