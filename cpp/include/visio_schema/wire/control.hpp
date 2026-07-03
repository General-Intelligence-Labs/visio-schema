// Control-stream id boundary — the one source for the control/data split.
// Stream ids below kFirstDynamic are the reserved control-plane block; ids
// at/above it are dynamic data streams. Sourced from the generated ControlStream
// proto enum.
//
// Control streams split by scope (a control id is a shared constant, NEVER
// remapped, so it can't disambiguate devices once forwarded):
//   * link-scoped (IsLinkLocalControl — heartbeat): per-hop, carries no device
//     identity, dropped at the hop.
//   * end-to-end (device_info, command): forwarded across hops, so each MUST
//     carry a device-identity field in its payload (source for announce, target
//     for directed control).
#pragma once

#include <cstdint>

#include "visio_schema/v1/wire/header.pb.h"

namespace visio_schema {

inline constexpr std::uint32_t kFirstDynamic = static_cast<std::uint32_t>(
    visio_schema_v1_wire_ControlStream_CONTROL_STREAM_FIRST_DYNAMIC);
inline constexpr std::uint32_t kDeviceInfo = static_cast<std::uint32_t>(
    visio_schema_v1_wire_ControlStream_CONTROL_STREAM_DEVICE_INFO);
inline constexpr std::uint32_t kHeartbeat = static_cast<std::uint32_t>(
    visio_schema_v1_wire_ControlStream_CONTROL_STREAM_HEARTBEAT);
inline constexpr std::uint32_t kCommand = static_cast<std::uint32_t>(
    visio_schema_v1_wire_ControlStream_CONTROL_STREAM_COMMAND);
inline constexpr std::uint32_t kExposureSync = static_cast<std::uint32_t>(
    visio_schema_v1_wire_ControlStream_CONTROL_STREAM_EXPOSURE_SYNC);

// True for control streams that never cross a hop (the bus drops them rather than
// relaying). The single source of truth for "link-scoped"; mirrors Python's
// LINK_LOCAL_CONTROL. A new control stream belongs here iff it is link-scoped and
// carries no device identity. Exposure-sync is single-hop hub↔child; a child
// source's grid is re-emitted to the other children by the hub's service (an
// explicit application-layer relay), never by bus auto-forwarding.
inline constexpr bool IsLinkLocalControl(std::uint32_t id) {
  return id == kHeartbeat || id == kExposureSync;
}

}  // namespace visio_schema
