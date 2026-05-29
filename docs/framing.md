# Visio Wire Framing — canonical spec

This document is the canonical wire spec for Visio messages. Both
implementations (`visio-mq/cpp/` and `visio-mq/python/`) MUST conform.
If anything in code disagrees with this document, this document wins.

## 1. Frame structure (overview)

Every Visio wire message consists of:

1. A small **`HEADER_LEN`** (2 bytes, little-endian unsigned 16-bit)
   giving the size of the serialized Header in bytes.
2. The **Header** — `visio.wire.v1.Header` protobuf payload, length
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
│ u16_le       │ (HEADER_LEN B) │ (variable)   │ u16_le        │
└──────────────┴────────────────┴──────────────┴───────────────┘
                ▲                ▲              ▲
                covered by CRC ──┴──────────────┘
                (HEADER_LEN itself is also covered)
```

## 2. The Header

`visio.wire.v1.Header` is a protobuf message — see
[`proto/visio/wire/v1/header.proto`](../proto/visio/wire/v1/header.proto):

```proto
message Header {
  DeviceClass               device       = 1;
  DeviceClass               routed_from  = 2;
  StreamKind                stream       = 3;
  uint32                    stream_index = 4;
  uint32                    seq          = 5;
  google.protobuf.Timestamp timestamp    = 6;
}
```

All enum values are constrained to `[0, 127]` so each enum field
encodes to exactly 2 bytes (1 tag + 1 varint value). `stream_index`
is semantically `uint8` (`[0, 255]`); values `0..127` encode as
1-byte varint, `128..255` as 2-byte varint.

Typical serialized Header size: **~21-25 bytes**.

`HEADER_LEN` is `u16` rather than `u8` so the Header can grow with
optional fields in future versions without a wire-format break.

## 3. Per-Endpoint frame wrappers

### 3.1 TCP

TCP is a byte stream with no native message boundaries; we add an
explicit total length prefix.

```
┌──────────────┬──────────────┬────────────┬──────────┬─────────┐
│ TOTAL_LEN    │ HEADER_LEN   │ header_pb  │ payload  │ CRC16   │
│ u32_le       │ u16_le       │ N bytes    │ M bytes  │ u16_le  │
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
│ u16_le       │ N bytes    │ M bytes  │ u16_le  │
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
[`stream_type_map.md`](stream_type_map.md) and the McapEndpoint
mapping table in
[`visio-schema/MASTER_PLAN.md`](../MASTER_PLAN.md#5-wire-header-v1-protobuf).

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
| Payload protobuf parse error | inner decode failure | Drop the message but do not drop the connection. Log at warn-level with `Header.stream` for triage. |
| Unknown `Header.stream` value | enum lookup in StreamKind table miss | Drop the message. Log at info-level (a peer may publish a stream we don't know yet — that's not an error, just no consumer). |
| `Header.device == DEVICE_UNKNOWN` or `Header.stream == STREAM_UNKNOWN` | obvious | Drop the frame; log at warn-level. Producers MUST set both fields. |

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

These checks live in `visio-mq/tests/interop/`.
