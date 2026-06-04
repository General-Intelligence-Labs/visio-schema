"""visio_schema.routing — stream identity: the id<->Channel<->topic table.

:class:`ChannelRegistry` is the single-source table a peer keeps (own outputs +
channels learned from DeviceInfo announces), with the unique-topic invariant. A
no-bus consumer uses it directly; on a bus the bus remaps every link into one id
space before the registry sees it.
"""
from visio_schema.routing.channel import Channel, DuplicateTopicError, Routed
from visio_schema.routing.registry import FIRST_DYNAMIC, ChannelRegistry

__all__ = [
    "Channel",
    "ChannelRegistry",
    "DuplicateTopicError",
    "FIRST_DYNAMIC",
    "Routed",
]
