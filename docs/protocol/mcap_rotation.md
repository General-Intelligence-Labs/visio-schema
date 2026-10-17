# MCAP video continuation parts

Firmware can opt into `McapRotationPolicy` on the C++ writer/endpoint. Generic
callers retain the previous policy unless they opt in. No protobuf fields change.

The writer prefers a cut on an actual local-camera IDR group. The caller supplies
local camera stream IDs explicitly. Compare the group's maximum minus minimum
corrected `Header.timestamp`; tolerance is the smaller of 1 ms and one quarter of
the shortest configured local frame period. Attached cameras do not gate rotation.
Their payloads, timestamps, sequences and legacy false keyframe flags survive.

A candidate holds shared payloads on the writer thread, for at most 100 ms and a
2 MiB accounted staging budget by default. Cancelled candidates drain back into
the old part. The existing byte ceiling (configured payload limit plus one eighth,
with an individual record allowed to cross it) forces a continuation cut even on a
P-frame. Recording continues. A duration-only policy has a bounded 100 ms grace.
Stop drains an incomplete candidate into the current part without opening an empty
successor. Local cameras still wait for their first session IDR; that initial gate
survives an early part cut. Attached cameras are retained from their first record.

Every opted-in part carries one standard MCAP Metadata record named
`visio.rotation`:

| Field | Value |
| --- | --- |
| `version` | `1` |
| `recording_id` | opaque random identity shared by this writer's parts |
| `part_index` | decimal index, beginning at zero |
| `video_continuation` | `true` |
| `cut_reason` | `initial`, `aligned`, or `forced` |

`visio.rotation` is reserved for the writer. Existing `visio.capture` metadata,
part names, schemas, payloads and durability/storage-authority operations remain
unchanged. VREC encryption wraps the same MCAP metadata and message stream.

## Reading

A continuation part is a complete MCAP container. Its video can depend on earlier
parts. Opening a continuation in isolation may yield no decoded video until its
next actual IDR. Existing readers that recreate codecs for each part can lose a
GOP at every forced cut; deploy the updated reader with this firmware.

`Session` orders an explicitly identified chain by `part_index`, even when an
attached camera backlog makes a later part's first timestamp older. It retains
codec contexts per camera across adjacent parts of the same recording. Protobuf
parse buffers/adapters are recreated for each file to release their parse arenas.
Unmarked legacy files keep their per-file decoder behavior. Custom video adapters
retain their existing per-file flush contract.

A missing part or changed recording identity resets the chain. A sequence gap,
source frame-ID change or codec format change resets the affected camera. CPU and
GPU decoders wait for a real bitstream IDR after reset; GPU in-flight tails flush
before reset and at chain end. Sequence wrapping is continuous. No GOP is copied
or duplicated. `log_time` remains the bus-corrected capture timestamp; payload
clock values are not substituted. `reader.ops.sync` and its unmatched-frame
semantics are unchanged.

## Qualification

Host tests cover exact records over more than 1,000 parts, encrypted cancellation,
alignment spread, stage timeout/overflow, startup gating, worker timeout/failure,
CPU and real NVDEC split/unsplit equivalence, timestamp pairing, source resets,
legacy/custom adapters, part ordering and 300 large-protobuf parts without retained
parse arenas. Hardware qualification remains a separate requirement.
