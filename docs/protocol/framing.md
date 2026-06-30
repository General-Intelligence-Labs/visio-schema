# Visio Wire Framing — canonical spec

This document is the canonical wire spec for Visio messages. Both
implementations (`cpp/` and `python/`) MUST conform.
If anything in code disagrees with this document, this document wins.

## 1. Frame structure (overview)

Every Visio wire message consists of:

1. A small **`HEADER_LEN`** (1 byte, unsigned 8-bit) giving the size
   of the serialized Header in bytes.
2. The **Header** — `visio_schema.v1.wire.Header` protobuf payload, length
   `HEADER_LEN` bytes.
3. The **payload** — the inner message bytes; protobuf-encoded per
   `Header.stream`'s mapped type (see
   [`stream_type_map.md`](stream_type_map.md)).
4. A **CRC-16/CCITT-FALSE** checksum (2 bytes, little-endian) covering
   `HEADER_LEN || header_pb || payload`.

Per-Endpoint frame wrappers add transport-specific framing around
this core (`TCP TOTAL_LEN`, `COBS` for serial, datagram boundaries for
UDP). See section 3.

```
core frame (always the same):
┌──────────────┬────────────────┬──────────────┬───────────────┐
│ HEADER_LEN   │ header_pb      │ payload      │ CRC16         │
│ u8           │ (HEADER_LEN B) │ (variable)   │ u16_le        │
└──────────────┴────────────────┴──────────────┴───────────────┘
                ▲                ▲              ▲
                covered by CRC ──┴──────────────┘
                (HEADER_LEN itself is also covered)
```

## 2. The Header

`visio_schema.v1.wire.Header` is a protobuf message — see
[`proto/visio_schema/v1/wire/header.proto`](../../proto/visio_schema/v1/wire/header.proto):

```proto
message Header {
  uint32                    stream_id = 1;  // per-link stream label
  uint32                    seq       = 2;  // per-stream_id sequence
  google.protobuf.Timestamp timestamp = 3;  // see timesync.md
}
```

A stream is named globally by its **topic** (e.g. `/glove_left/imus/3/raw`); the
wire carries only a compact per-link `stream_id`. A `ControlStream` enum splits
the id space: ids `[1, FIRST_DYNAMIC=16)` are hop-local control streams
(DEVICE_INFO=1, TIMESYNC=2, HEARTBEAT=3, COMMAND=4), ids `≥16` are negotiated
data streams that hubs remap. The `stream_id → (topic, payload type, schema)`
binding is learned at runtime from the periodic `DeviceInfo` announce — each
announced `Channel` carries its `schema_name` (protobuf full name) and `schema`
(serialized `FileDescriptorSet`) inline (see
[`stream_type_map.md`](stream_type_map.md)). `seq` is producer-owned, per
`stream_id`, and `timestamp` is rewritten on receive by the timesync offset (see
[`timesync.md`](timesync.md)).

Typical serialized Header size: **~10-14 bytes** (one 1-byte stream_id varint,
one seq varint, and the ~8-byte Timestamp submessage).

`HEADER_LEN` is a single byte: the ~21-25 byte Header never approaches
255 bytes, and it grows *compatibly* via optional protobuf fields, so a
wider length field buys nothing. There is also **no separate header
version byte** — structural breaks are handled by the proto package
version (`visio_schema.v1.wire` → `v2`). (This field was `u16` in an earlier
draft; the narrowing is a deliberate pre-1.0 wire change.)

## 3. Per-Endpoint frame wrappers

### 3.1 TCP

TCP is a byte stream with no native message boundaries; we add an
explicit total length prefix.

```
┌──────────────┬──────────────┬────────────┬──────────┬─────────┐
│ TOTAL_LEN    │ HEADER_LEN   │ header_pb  │ payload  │ CRC16   │
│ u32_le       │ u8           │ N bytes    │ M bytes  │ u16_le  │
└──────────────┴──────────────┴────────────┴──────────┴─────────┘
```

- `TOTAL_LEN` = `2 + N + M + 2` (everything after `TOTAL_LEN`).
- Reader: read 4 bytes → `TOTAL_LEN`, then read exactly `TOTAL_LEN`
  more bytes; parse the resulting buffer.
- `TOTAL_LEN` is NOT covered by CRC (it's a framing artifact; corruption
  there causes a length-mismatch error rather than a silent CRC pass).

### 3.2 Serial (USB CDC, UART) — COBS-framed

Serial transports get [COBS](https://en.wikipedia.org/wiki/Consistent_Overhead_Byte_Stuffing)
delimiters so frames are self-delimiting on a byte stream without a
length prefix. The decoded inside-of-COBS bytes are
`HEADER_LEN || header_pb || payload || CRC16`.

```
on the wire:
┌─────────────────────────────────────────────────────────────────┐ ┌──────┐
│ COBS-encode( HEADER_LEN || header_pb || payload || CRC16 )      │ │ 0x00 │
└─────────────────────────────────────────────────────────────────┘ └──────┘
```

- The trailing `0x00` is the frame delimiter; the COBS encoding
  guarantees no other `0x00` bytes appear in the encoded run.
- Reader: read until `0x00`, COBS-decode, then parse the decoded
  bytes the same way as TCP (minus the outer `TOTAL_LEN`).
- COBS encoding overhead is at most `ceil(N / 254) + 1` bytes — in
  practice ~0.4 % for the frame sizes we use.

### 3.3 UDP

Each Visio message is exactly one UDP datagram. No `TOTAL_LEN` is
needed (datagram boundaries are intrinsic).

```
┌──────────────┬────────────┬──────────┬─────────┐
│ HEADER_LEN   │ header_pb  │ payload  │ CRC16   │
│ u8           │ N bytes    │ M bytes  │ u16_le  │
└──────────────┴────────────┴──────────┴─────────┘
```

Datagrams larger than the path MTU are dropped — fragmenting Visio
messages across UDP packets is NOT supported. Use TCP, WebSocket,
or MCAP for large payloads.

### 3.4 WebSocket

WebSocket frames are self-delimiting at the protocol level. No
Visio-side framing; the WebSocket frame payload IS the core frame
`HEADER_LEN || header_pb || payload || CRC16`. CRC is kept (cheap
defense in depth; WebSocket's transport-level integrity is
TCP+TLS-based, which is independent of our application-level integrity).

### 3.5 MCAP

MCAP is not a transport in the same sense — it's a structured
container. The Header and payload do NOT serialize together on disk.
McapEndpoint maps Header fields onto MCAP's own record fields; see
[`stream_type_map.md`](stream_type_map.md) and
[`foxglove_compat.md`](foxglove_compat.md) for the MCAP schema-name mapping.

## 4. CRC-16/CCITT-FALSE

| Parameter | Value |
|---|---|
| Polynomial | `0x1021` |
| Initial value | `0xFFFF` |
| Input reflection | none |
| Output reflection | none |
| XOR-out | none |
| Check value (`"123456789"`) | `0x29B1` |

This is also known as CRC-16/AUTOSAR, CRC-16/IBM-3740, and CRC-CCITT
in some references. It's the de facto industrial default.

Coverage: `HEADER_LEN || header_pb || payload`. The CRC bytes
themselves are NOT covered (they're the result).

Reference implementations:
- C: [crcmod tables](https://crccalc.com/) (pick CRC-16/CCITT-FALSE)
- Python: `crcmod.predefined.Crc('crc-ccitt-false')` or
  `crc.Calculator(crc.Crc16.CCITT_FALSE)` (`pip install crc`)
- Embedded: ~20-line table-driven implementation; verified by the
  `0x29B1` check value above.

## 5. Error handling

| Failure mode | Detection | Reader behavior |
|---|---|---|
| CRC mismatch | computed CRC ≠ trailing CRC | Drop the frame; log at debug-level; resume scanning. No retransmit at this layer. |
| `HEADER_LEN` larger than available bytes | TCP/UDP: length math; Serial: decoded length > buffer | Drop the frame; log at warn-level. |
| Header protobuf parse error | protobuf decode failure | Drop the frame; log at warn-level. |
| Payload protobuf parse error | inner decode failure | Drop the message but do not drop the connection. Log at warn-level with `Header.stream_id` for triage. |
| Unmapped data `stream_id` | no `(source, id)` mapping yet (DeviceInfo announce not processed) | Drop the message (drop-until-mapped); count it (`dropped_unmapped`). The next announce (≤ `announce_interval_s`) resolves it. Not an error. |
| `Header.stream_id == 0` | `CONTROL_STREAM_INVALID` | Drop the frame; log at warn-level. Producers MUST set a valid control id or a declared data id. |

Endpoints MUST NOT silently swallow CRC failures or shape errors —
they must log them. The default log level for "frame dropped" is
implementation-defined but defensible to a reviewer.

## 6. Endpoint conformance checklist

An Endpoint implementation conforms to this spec when, given a
correctly-framed test fixture for its transport:

1. It parses the same Header bytes the spec describes (verifiable by
   round-tripping a known `Header` proto).
2. It computes CRC-16/CCITT-FALSE using the polynomial / init above
   (verifiable against the `0x29B1` check value).
3. On a deliberately corrupted byte, it drops the frame rather than
   surfacing a corrupted payload.
4. On cross-Endpoint interop (a C++ Endpoint pairing with a Python
   Endpoint over a real Link), every frame produced by one is
   parsed identically by the other.

These checks live in the bus/transport layer's interop tests.
