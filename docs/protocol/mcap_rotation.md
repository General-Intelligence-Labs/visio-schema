# MCAP video continuation parts

Firmware opts into `McapRotationPolicy` on the C++ writer/endpoint. Recording
control remains outside `SyncService`; the service supplies the next regular IDR
identity and the writer confirms the cut from received video.

At start, the caller freezes the selected video stream IDs, including attached
wrist cameras. Membership follows camera topics: reconnecting the same camera
retains its recording channel despite a new bus stream ID. Cameras with new
topics discovered during a take wait until the next take. Every
selected camera must supply an actual independently decodable IDR carrying the
same `Header.sync_point` (authority epoch, grid generation and frame index).
The maximum minus minimum corrected `Header.timestamp` must be within 1 ms.
Timestamp proximity alone cannot join different IDR groups.

Storage preparation completes before the caller arms a future regular IDR slot.
Delayed frames from earlier slots cannot open the take. Initial video before the
target is discarded; the writer reports ready only after the complete group is
staged. The start deadline is 1100 ms, including a default 100 ms arrival window;
no extra IDR is requested. Missing cameras, identity changes, excessive timestamp
spread, staging overflow or timeout fail the start. The caller closes the writer
and removes its empty session so the command can be retried.

Rotation stages a candidate with shared payload ownership on the writer thread,
within a default 8 MiB budget and 100 ms arrival window. Earlier frames for a
camera stay in the old part; its matching IDR and successors enter the new part.
A cancelled candidate drains back to the old part without dropping records.
The payload byte ceiling (configured limit plus one eighth, with one record
allowed to cross it) forces a continuation cut when alignment is unavailable.
A duration-only policy has a bounded one-second IDR wait plus the arrival window.
Stop drains an incomplete candidate to the current part without an empty successor.

Every opted-in part carries a standard MCAP Metadata record named `visio.rotation`:

| Field | Value |
| --- | --- |
| `version` | `1` |
| `recording_id` | opaque random identity shared by this writer's parts |
| `part_index` | decimal index, beginning at zero |
| `video_continuation` | `false` for initial/aligned parts; `true` for a forced cut |
| `cut_reason` | `initial`, `aligned`, or `forced` |
| `authority_epoch`, `grid_generation`, `frame_index` | confirmed identity for aligned cuts; ignore on forced cuts |

A successful initial cut also writes `visio.sync.start` with the shared identity
and selected stream IDs. `visio.rotation` is reserved for the writer. Existing
`visio.capture` metadata, part names, schemas, payloads and durability/storage
authority operations retain their contracts. VREC wraps the same MCAP stream.

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

A missing part, changed recording identity, or independently decodable
(`video_continuation=false`) part resets the decoder chain. A sequence gap,
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
