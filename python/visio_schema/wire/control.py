"""Control-stream id constants — the one source for the control/data boundary.

Stream ids below ``FIRST_DYNAMIC`` are the reserved control-plane block; ids
at/above it are dynamic data streams. Sourced from the generated
``ControlStream`` proto enum so there is a single source of truth shared by the
registry, the bus, and the services.

Control streams split by **scope**, which decides whether the bus forwards them
across a hop (a control id is a shared constant that is NEVER remapped):

* **link-scoped** (``LINK_LOCAL_CONTROL`` — heartbeat): describes the hop between
  two directly-connected peers (RTT, clock offset), carries no device identity,
  and is dropped at the hop.
* **end-to-end** (device_info, command): forwarded across hops, so each MUST
  carry a device-identity field in its payload (source for announce/telemetry,
  target for directed control) since the stream id can't disambiguate them.
"""
from visio_schema.wire.v1.header_pb2 import ControlStream

FIRST_DYNAMIC = ControlStream.CONTROL_STREAM_FIRST_DYNAMIC
DEVICE_INFO = ControlStream.CONTROL_STREAM_DEVICE_INFO
HEARTBEAT = ControlStream.CONTROL_STREAM_HEARTBEAT
COMMAND = ControlStream.CONTROL_STREAM_COMMAND

# Control streams that never cross a hop (the bus drops them rather than relaying).
# A new control stream belongs here iff it is link-scoped and carries no device
# identity; end-to-end control (device_info, command) is left out and forwarded.
LINK_LOCAL_CONTROL = frozenset({HEARTBEAT})

__all__ = [
    "COMMAND",
    "DEVICE_INFO",
    "FIRST_DYNAMIC",
    "HEARTBEAT",
    "LINK_LOCAL_CONTROL",
]
