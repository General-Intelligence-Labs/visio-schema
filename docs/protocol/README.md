# Visio protocol reference

These documents are the **normative wire contract** for Visio. They define the bytes on the link,
how streams are named and discovered, how clocks are synchronized, and what a version bump means.
Anything that talks Visio — the device firmware, the Visio bus, this package's codec, and any
third-party client you write — must conform to what's here. If you only want to *use* the Python
package, start with [`../usage.md`](../usage.md); read these when you implement or debug a client.

| Document | What it specifies |
|---|---|
| [`framing.md`](framing.md) | The byte-level wire frame: `HEADER_LEN \| header_pb \| payload \| CRC16`, the per-transport wrappers (COBS on TCP and serial, UDP datagrams, MCAP), and the CRC-16/CCITT-FALSE algorithm with test vectors. |
| [`stream_type_map.md`](stream_type_map.md) | Dynamic streams: how a compact `stream_id` maps to a topic + payload type, learned at runtime from the device's `DeviceInfo` announce (no compile-time enum table). Topic naming convention. |
| [`timesync.md`](timesync.md) | The NTP-style clock-offset algorithm folded into the heartbeat beacon, the sliding-window RTT filter, and the receive-side timestamp rewrite. (To *use* it rather than implement it, see [`../timesync_client.md`](../timesync_client.md).) |
| [`foxglove_compat.md`](foxglove_compat.md) | Which [Foxglove](https://foxglove.dev) schema types Visio adopts as-is, which it mirrors, and which it defines itself — and how MCAP schema names are chosen so Foxglove Studio resolves them. |
| [`versioning.md`](versioning.md) | The semver policy (what bumps PATCH / MINOR / MAJOR), the per-package `vN` strategy, and the `buf` breaking-change checks. The Python public API surface is pinned separately (see [`../../AGENTS.md`](../../AGENTS.md)). |
| [`storage-providers.md`](storage-providers.md) | The customer storage destination `SetStorage` names: how AWS S3 / Aliyun OSS / Tencent COS / Google Cloud Storage / Azure Blob are told apart from the endpoint host, and what each one's signature, bucket addressing, overwrite guard and list version are. Also the shared list+put permission contract. |
| [`ota.md`](ota.md) | Firmware update: the `CONTROL_STREAM_OTA` session (begin / windowed chunks / commit) and what `bytes_received` means, the chunk-size negotiation and its floor and ceiling, the `OtaBegin.board` mark, and the multi-board PACKAGE — a stored ustar whose index comes first and whose own image comes last. `visio_schema.wire.ota` is the reference driver; `tests/golden/ota_vectors.txt` pins every implementation of it. |
| [`recordings_pull.md`](recordings_pull.md) | Copying recorded sessions off a device: `ListRecordings` paging and per-file detail (the `writing` flag on a file still being recorded), `OpenRecordingFile` and its lease, the bare TCP socket that carries the bytes (connect, read to close, never write), resume after a short read, `DeleteRecording`, and the record-while-pulling policy. `visio_schema.wire.recordings` is the reference client; `tests/golden/recordings_wire_vectors.txt` pins the codecs. |
| [`diag_log.md`](diag_log.md) | The device's own diagnostic log: the stamped one-line record format, the `VDLG`/`VDLW` containers (the recording container under a different `CipherSuite`), `bytes_valid` as the tear detector, the self-identifying file names on flash and at the SD-card root, and the three ways a log reaches us — `CONTROL_STREAM_DIAG`, `/<root>/diag_log` inside every recording, or the card itself. `visio_schema.diag` is the reference parser. |

The codec in `visio_schema.wire.codec` is the executable form of `framing.md`; the
golden test vectors in `python/tests` and `cpp/tests` tie the implementations to these specs.
