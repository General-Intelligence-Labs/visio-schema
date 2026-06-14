"""visio_schema.mcap — the MCAP container codec (the Foxglove format) + its endpoints.

:class:`McapWriter` writes a spec-conformant, Foxglove-readable file from
``(channel, message)`` pairs; :func:`read_mcap` reads one back as the same pairs,
so a recording round-trips through the same shape a live
:meth:`ChannelRegistry.resolved` stream produces. :class:`McapWriterEndpoint` is the
active-object sink that records bus traffic; :class:`McapReaderEndpoint` is its replay
source — it streams a recording in place of a live link so downstream is unchanged.
``mcap`` is an optional dependency: ``pip install visio-schema[mcap]``.
"""
from visio_schema.mcap.reader import read_mcap
from visio_schema.mcap.reader_endpoint import McapReaderEndpoint
from visio_schema.mcap.writer import McapWriter
from visio_schema.mcap.writer_endpoint import McapWriterEndpoint

__all__ = ["McapReaderEndpoint", "McapWriter", "McapWriterEndpoint", "read_mcap"]
