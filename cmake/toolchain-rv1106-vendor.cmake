# CMake cross toolchain for the RV1106 HDK (vendor SDK).
#
# Targets the same triple the umi_embedded firmware uses:
#   arm-rockchip830-linux-uclibcgnueabihf  (32-bit ARMv7-A + NEON, uClibc)
#
# Point VISIO_RV1106_TOOLCHAIN at the SDK toolchain root that contains
# bin/arm-rockchip830-linux-uclibcgnueabihf-{gcc,g++}. Example:
#   cmake -S cpp -B build-rv1106 \
#     -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-rv1106-vendor.cmake \
#     -DVISIO_RV1106_TOOLCHAIN=/path/to/rk_rv1106_ipc_linux/tools/linux/toolchain/arm-rockchip830-linux-uclibcgnueabihf

set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)

set(_triple arm-rockchip830-linux-uclibcgnueabihf)

if(NOT VISIO_RV1106_TOOLCHAIN)
  set(VISIO_RV1106_TOOLCHAIN "$ENV{VISIO_RV1106_TOOLCHAIN}")
endif()
if(NOT VISIO_RV1106_TOOLCHAIN)
  message(FATAL_ERROR
    "Set -DVISIO_RV1106_TOOLCHAIN=<sdk>/tools/linux/toolchain/${_triple} "
    "(or export it as an env var)")
endif()

# CMake re-runs this toolchain file inside its compiler-ABI try_compile, but
# does not forward -D cache vars to that sub-project. Forward this one so the
# probe can still find the compiler.
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES VISIO_RV1106_TOOLCHAIN)

set(_bin "${VISIO_RV1106_TOOLCHAIN}/bin")
set(CMAKE_C_COMPILER   "${_bin}/${_triple}-gcc")
set(CMAKE_CXX_COMPILER "${_bin}/${_triple}-g++")

# Match the firmware's ARMv7 + NEON hard-float flags.
set(_arch "-march=armv7-a -mfpu=neon -mfloat-abi=hard")
set(CMAKE_C_FLAGS_INIT   "${_arch}")
set(CMAKE_CXX_FLAGS_INIT "${_arch}")

# This toolchain produces only static libs for the device, and its linker
# rejects CMake's default `-rdynamic` exe-link probe. Compile-test as a static
# library so the compiler check skips the (unsupported) link step.
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
