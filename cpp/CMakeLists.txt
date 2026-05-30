cmake_minimum_required(VERSION 3.20)
project(visio_schema CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE RelWithDebInfo)
endif()

# ---- protobuf --------------------------------------------------------------
# Hint to conda's protobuf install. Override CMAKE_PREFIX_PATH on the command
# line to use a different install (system, vcpkg, etc.).
if(DEFINED ENV{CONDA_PREFIX})
  list(APPEND CMAKE_PREFIX_PATH "$ENV{CONDA_PREFIX}")
endif()
find_package(Protobuf CONFIG REQUIRED)
find_package(absl CONFIG REQUIRED)

# ---- generated bindings ----------------------------------------------------
# `make gen` emits the .pb.cc/.pb.h into cpp/generated (in-package, next to
# the hand-written codec). They compile straight into this library so
# consumers link ONE target for both the generated bindings and the codec.
set(VISIO_GEN_CPP "${CMAKE_CURRENT_SOURCE_DIR}/generated")
if(NOT EXISTS "${VISIO_GEN_CPP}/visio_schema/wire/v1/header.pb.h")
  message(FATAL_ERROR
    "visio-schema generated C++ bindings not found at ${VISIO_GEN_CPP}.\n"
    "Run `make gen` in the repo root first.")
endif()
file(GLOB_RECURSE VISIO_GEN_SRCS CONFIGURE_DEPENDS "${VISIO_GEN_CPP}/*.pb.cc")

# ---- the one wholistic library: bindings + framing codec -------------------
add_library(visio_schema STATIC
  ${VISIO_GEN_SRCS}
  src/codec/crc16.cc
  src/codec/cobs.cc
  src/codec/frame.cc
)
target_include_directories(visio_schema PUBLIC
  "${VISIO_GEN_CPP}"
  "${CMAKE_CURRENT_SOURCE_DIR}/include"
)
target_link_libraries(visio_schema PUBLIC protobuf::libprotobuf)
add_library(visio::schema ALIAS visio_schema)

# ---- tests (top-level build only) ------------------------------------------
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
  set(_visio_schema_tests_default ON)
else()
  set(_visio_schema_tests_default OFF)
endif()
option(VISIO_SCHEMA_BUILD_TESTS "Build visio-schema codec tests"
       ${_visio_schema_tests_default})

if(VISIO_SCHEMA_BUILD_TESTS)
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
