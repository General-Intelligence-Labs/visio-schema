# Manual camera snapshots

One `CaptureSnapshot {}` command solicits one finite JPEG response stream on
BLE, TCP (Wi-Fi or USB-NCM), or USB serial. There are no per-chunk commands.
The command must address the directly connected device and have a nonzero
`command_id`. Empty targets and hub-relayed targets return `invalid_request`;
ordinary hub relay does not preserve the requesting connection.

The device announces `/<device>/snapshot` with schema
`visio_schema.v1.service.snapshot.SnapshotChunk`. Register a collector before
sending the capture command. The command returns one `CommandResult.snapshot`
descriptor containing `snapshot_id`, `camera_index`, `width`, `height`,
`capture_pts_us`, and `total_bytes`. Its `snapshot_id` equals the capture's
`command_id`. Chunks carry this ID, a byte `offset`, and 1..1024 JPEG bytes.
They can arrive before or after the descriptor. They go only to the requesting
connection through the bus's existing link routing, rather than publication
to other live clients or recordings.

Offsets start at zero and increase contiguously. A receiver succeeds only after
the metadata and exactly `total_bytes` have arrived, with a complete decodable
JPEG. Missing, overlapping, out-of-order, malformed or excess bytes fail the
capture. A disconnect or deadline also fails it; request a fresh capture rather
than retrying byte reads. There is no cache, lease, read command, cancel command,
extra socket, file, or GATT characteristic.

The image defaults to an upright 640×360 JPEG at quality 60. The full camera
field of view is scaled to fit, with black bars when its aspect ratio differs
from 16:9. Capture leaves the configured camera resolution alone and creates
no recording. It works without SD storage, cloud reporting or hand detection.
It wakes cameras in video standby and uses the existing bounded frame settling
before sampling. During OTA camera quiescence it returns `busy`.
`DeviceState.snapshot_supported` is true only when the camera capture source
initialized; its proto3 default is false on older or camera-less firmware.

`camera_index` is the actual configured primary camera index, which can be
nonzero. `capture_pts_us` is original VI capture PTS in CLOCK_BOOTTIME
microseconds, rather than UTC or the timesynchronized bus header timestamp.

The device admits one capture and stream-enqueue operation at a time, holds at
most 128 KiB of transient JPEG bytes, and bounds waiting for queue capacity to
30 seconds. The receiver also bounds the whole operation to 30 seconds. The
transport accepts chunks into a separate non-evicting response queue of at most 8 KiB
queued bytes plus one in-flight frame. When full, the worker waits for local
queue capacity; it does not ask the host for another command. Normal control
messages take precedence at complete wire-frame boundaries. ATT fragmentation
and MTU negotiation remain transport responsibilities. BLE explicitly admits
the snapshot topic; the companion exempts finite snapshot chunks from native
preview rate caps. BLE also requests a 2 KiB socket send buffer to keep kernel
backlog small enough for controls to interleave during transfer.

Disconnect invalidates capture work and discards queued response bytes. A local
UI abort removes the collector and ignores late chunks; the one-shot device
response may finish. It keeps no unfinished-reader reservation. When queue
capacity does not recover within 30 seconds, the worker discards queued bytes
and releases capture admission. The self-healing device CDC attachment cannot
identify every host TTY close; this bound applies even when that close produces
no bus detach.

The companion retains one routed session throughout readiness, the command
and reception, then releases it and removes its collector. The BLE panel opens
closed, captures once on Open, and captures again only on Refresh. Hide,
background, navigation, disconnect or route changes abort local reception.
A failed refresh keeps the previous successfully decoded image visible.
