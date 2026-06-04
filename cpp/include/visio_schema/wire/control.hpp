// Control-stream id boundary — the one source for the control/data split.
// Stream ids below kFirstDynamic are the reserved control-plane block (hop-local,
// never relayed); ids at/above it are dynamic data streams. Sourced from the
// generated ControlStream proto enum.
#pragma once

#include <cstdint>

#include "visio_schema/wire/v1/header.pb.h"

namespace visio_schema {

inline constexpr std::uint32_t kFirstDynamic = static_cast<std::uint32_t>(
    visio_schema_wire_v1_ControlStream_CONTROL_STREAM_FIRST_DYNAMIC);

}  // namespace visio_schema
