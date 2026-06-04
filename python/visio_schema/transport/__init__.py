"""visio_schema.transport — endpoints + links that move Messages over a stream.

A schema-only user reads from / writes to ONE visio stream with these and no bus:
open a :class:`Link`, wrap it in a :class:`SerialEndpoint`, and drive
``try_read()`` / ``write()``. Every Endpoint lives here — live byte links
(`SerialEndpoint`) and the MCAP sink (`McapEndpoint`, which wraps
:class:`visio_schema.mcap.McapWriter`). Endpoints do NOT reconnect — on a broken
link they raise :class:`EndpointClosed`; the caller decides what to do.
"""
from visio_schema.transport.endpoint import Endpoint, EndpointClosed
from visio_schema.transport.framing import extract_frames, frame_bytes, read_frames
from visio_schema.transport.link import FdLink, Link
from visio_schema.transport.mcap_endpoint import McapEndpoint
from visio_schema.transport.queue import QueueEndpoint
from visio_schema.transport.serial import SerialEndpoint

__all__ = [
    "Endpoint",
    "EndpointClosed",
    "FdLink",
    "Link",
    "McapEndpoint",
    "QueueEndpoint",
    "SerialEndpoint",
    "extract_frames",
    "frame_bytes",
    "read_frames",
]
