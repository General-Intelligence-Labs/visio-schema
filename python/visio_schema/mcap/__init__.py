"""visio_schema.mcap — the MCAP container codec (the Foxglove format).

:class:`McapWriter` writes a spec-conformant, Foxglove-readable file from
``(channel, message)`` pairs; :func:`read_mcap` reads one back as the same pairs,
so a recording round-trips through the same shape a live
:meth:`ChannelRegistry.resolved` stream produces. The transport-layer
:class:`~visio_schema.transport.mcap_endpoint.McapEndpoint` wraps the writer to
record bus traffic. ``mcap`` is an optional dependency:
``pip install visio-schema[mcap]``.
"""
from visio_schema.mcap.reader import read_mcap
from visio_schema.mcap.writer import McapWriter

__all__ = ["McapWriter", "read_mcap"]
