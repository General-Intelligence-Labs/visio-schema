# Visio protocol reference

These documents are the **normative wire contract** for Visio. They define the bytes on the link,
how streams are named and discovered, how clocks are synchronized, and what a version bump means.
Anything that talks Visio — the device firmware, the Visio bus, this package's codec, and any
third-party client you write — must conform to what's here. If you only want to *use* the Python
package, start with [`../usage.md`](../usage.md); read these when you implement or debug a client.

| Document | What it specifies |
|---|---|
| [`framing.md`](framing.md) | The byte-level wire frame: `HEADER_LEN \| header_pb \| payload \| CRC16`, the per-transport wrappers (TCP length prefix, serial COBS, MCAP), and the CRC-16/CCITT-FALSE algorithm with test vectors. |
| [`stream_type_map.md`](stream_type_map.md) | Dynamic streams: how a compact `stream_id` maps to a topic + payload type, learned at runtime from the device's `DeviceInfo` announce (no compile-time enum table). Topic naming convention. |
| [`timesync.md`](timesync.md) | The NTP-style clock-offset algorithm folded into the heartbeat beacon, the sliding-window RTT filter, and the receive-side timestamp rewrite. (To *use* it rather than implement it, see [`../timesync_client.md`](../timesync_client.md).) |
| [`foxglove_compat.md`](foxglove_compat.md) | Which [Foxglove](https://foxglove.dev) schema types Visio adopts as-is, which it mirrors, and which it defines itself — and how MCAP schema names are chosen so Foxglove Studio resolves them. |
| [`versioning.md`](versioning.md) | The semver policy (what bumps PATCH / MINOR / MAJOR), the per-package `vN` strategy, and the `buf` breaking-change checks. The Python public API surface is pinned separately (see [`../../AGENTS.md`](../../AGENTS.md)). |

The codec in `visio_schema.wire.codec` is the executable form of `framing.md`; the
golden test vectors in `python/tests` and `cpp/tests` tie the implementations to these specs.
