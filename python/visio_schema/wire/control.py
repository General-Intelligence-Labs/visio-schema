"""Control-stream id constants — the one source for the control/data boundary.

Stream ids below ``FIRST_DYNAMIC`` are the reserved control-plane block
(hop-local, never relayed); ids at/above it are dynamic data streams. Sourced
from the generated ``ControlStream`` proto enum so there is a single source of
truth shared by the registry, the bus, and the services.
"""
from visio_schema.wire.v1.header_pb2 import ControlStream

FIRST_DYNAMIC = ControlStream.CONTROL_STREAM_FIRST_DYNAMIC
DEVICE_INFO = ControlStream.CONTROL_STREAM_DEVICE_INFO
HEARTBEAT = ControlStream.CONTROL_STREAM_HEARTBEAT
COMMAND = ControlStream.CONTROL_STREAM_COMMAND

__all__ = ["FIRST_DYNAMIC", "DEVICE_INFO", "HEARTBEAT", "COMMAND"]
