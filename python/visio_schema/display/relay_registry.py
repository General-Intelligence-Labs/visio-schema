"""RelayRegistry — a ChannelRegistry for consuming a *relayed multiplex* stream.

Lives in ``display`` (not ``routing``) because it is a viewer-side policy, not a
general routing primitive: only the relay-consumer paths use it (the ``--tcp``
viewer and the foxglove bridge in this package).

The base :class:`ChannelRegistry` is single-source: it assumes one link's id
space and that some bus calls :meth:`~ChannelRegistry.forget` on link-drop so a
topic frees up for a reconnect (see the registry module docstring). A host viewer
of a relay leg — e.g. the Quest's ``:50002`` stream, which multiplexes many
devices' channels onto one TCP link — has **no per-device link-drop signal**: the
one TCP link stays up across a device reconnect. So a device that reconnects
re-announces its topics under *new* ids while the base registry still holds the
stale ids, and :meth:`~ChannelRegistry.learn` rejects the new ids as a
duplicate-topic fault. The reconnected device's data (on the new ids) then never
resolves — its stream goes silent for the viewer.

``RelayRegistry`` closes that gap **without weakening the unique-topic
invariant**: the re-announce itself is the forget trigger. A ``DeviceInfo``
announce is a full snapshot of one device's channels and carries its
``device_name``, so when the *same* device re-announces a topic under a new id,
the old id is dead — forget it, then learn the new. A *different* device
announcing an already-mapped topic is still a real collision (e.g. two same-side
grippers) and still raises → logged, exactly as the base registry surfaces it.

Only the relay-consumer path uses this; serial and MCAP replay keep the strict
single-source base registry.
"""
from __future__ import annotations

import logging

from visio_schema.routing.channel import DuplicateTopicError
from visio_schema.routing.registry import ChannelRegistry, DeviceInfo

log = logging.getLogger(__name__)


class RelayRegistry(ChannelRegistry):
    """A :class:`ChannelRegistry` that tolerates device reconnects on a relayed
    multiplex stream by treating each device's announce as authoritative for that
    device's channels. See the module docstring."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # learned id -> the device_name whose announce introduced it, so a
        # re-announce from the SAME device can forget its own stale ids while a
        # DIFFERENT device claiming the topic still collides.
        self._owner: dict[int, str] = {}

    def on_announce(self, payload: bytes) -> None:
        di = DeviceInfo()
        try:
            di.ParseFromString(payload)
        except Exception:
            log.warning("dropping malformed DeviceInfo announce")
            return
        dev = di.device_name
        for ch in di.channels:
            existing = self._topic_to_id.get(ch.topic)
            if (
                existing is not None
                and existing != ch.id
                and self._owner.get(existing) == dev
            ):
                # Same device, same topic, new id: it reconnected — the old id is
                # dead. Warn (a reconnect is worth noticing), free it, then learn.
                log.warning(
                    "device %r reconnected: topic %r id %d -> %d (dropping stale id)",
                    dev, ch.topic, existing, ch.id,
                )
                self.forget([existing])
                self._owner.pop(existing, None)
            try:
                self.learn(ch)
            except DuplicateTopicError as exc:
                # A *different* device claims a live topic — a genuine collision
                # (e.g. two same-side grippers). Keep surfacing it, don't remap.
                log.error("announce: %s", exc)
                continue
            self._owner[ch.id] = dev
