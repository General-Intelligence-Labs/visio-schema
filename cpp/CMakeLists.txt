cmake_minimum_required(VERSION 3.20)
project(visio_schema CXX C)

set(CMAKE_CXX_STANDARD 17)  # embeddable target is gcc 8.3 / C++17 (no <span>)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

# The embeddable wire library is nanopb-only — no full libprotobuf, no abseil —
# so it cross-compiles for the RV1106 (HDK / vendor SDK, uClibc armv7). The wire
# codec only (de)serializes the Header, but a device ORIGINATES payloads (camera
# frames, IMU/encoder batches, DeviceInfo, ...) and needs their nanopb structs
# too — so we compile the WHOLE generated tree. Per-field encoding (zero-copy
# callback for big blobs, bounded inline for small fields, pointer for the
# once-per-run DeviceInfo Response) is set in proto/nanopb.options. Host-side
# full-protobuf tooling lives in the Python package.

set(NANOPB_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/nanopb")
set(NANOPB_GEN "${CMAKE_CURRENT_SOURCE_DIR}/generated_nanopb")

if(NOT EXISTS "${NANOPB_GEN}/visio_schema/wire/v1/header.pb.h")
  message(FATAL_ERROR
    "nanopb bindings not found at ${NANOPB_GEN}. Run `make gen` first.")
endif()
if(NOT EXISTS "${NANOPB_DIR}/pb_encode.c")
  message(FATAL_ERROR
    "nanopb runtime not found at ${NANOPB_DIR}. "
    "Run `git submodule update --init third_party/nanopb`.")
endif()

# Every generated payload type, not just the Header. CONFIGURE_DEPENDS re-globs
# when the set changes (e.g. a new proto added to the schema).
file(GLOB_RECURSE NANOPB_GEN_SRCS CONFIGURE_DEPENDS "${NANOPB_GEN}/*.pb.c")

add_library(visio_schema STATIC
  # nanopb runtime
  "${NANOPB_DIR}/pb_encode.c"
  "${NANOPB_DIR}/pb_decode.c"
  "${NANOPB_DIR}/pb_common.c"
  # nanopb-generated bindings for the whole schema (Header + all payloads)
  ${NANOPB_GEN_SRCS}
  # hand-written codec
  src/codec/crc16.cc
  src/codec/cobs.cc
  src/codec/frame.cc
)
target_include_directories(visio_schema PUBLIC
  "${NANOPB_DIR}"
  "${NANOPB_GEN}"
  "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
add_library(visio::schema ALIAS visio_schema)

# ---- tests (top-level host builds only; skipped when cross-compiling) -------
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR AND NOT CMAKE_CROSSCOMPILING)
  include(FetchContent)
  FetchContent_Declare(
    googletest
    URL https://github.com/google/googletest/archive/refs/tags/v1.15.2.tar.gz
    URL_HASH SHA256=7b42b4d6ed48810c5362c265a17faebe90dc2373c885e5216439d37927f02926
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
  )
  set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)
  FetchContent_MakeAvailable(googletest)

  enable_testing()
  include(GoogleTest)

  add_executable(visio_schema_tests
    tests/test_crc16.cc
    tests/test_cobs.cc
    tests/test_frame_roundtrip.cc
  )
  target_link_libraries(visio_schema_tests
    PRIVATE visio_schema GTest::gtest GTest::gtest_main)
  gtest_discover_tests(visio_schema_tests)
endif()
