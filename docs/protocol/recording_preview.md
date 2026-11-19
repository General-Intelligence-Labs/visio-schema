# Recording preview

A device can store a short preview video of its primary camera with each
recording, and a host downloads it whole. It shows what a recording holds
without pulling the recording itself, which BLE cannot carry. For example, the
companion app plays it and asks the wearer's consent before the recording is
uploaded.

## On the device

The recording preview is a device setting (`[recording_preview]` in the
firmware configuration), off unless enabled. While a recording is being written
to the card, the device encodes one frame of its primary camera every
configured interval (10 s by default) and appends it to the preview. A frame
that cannot be taken in its turn (camera busy with a snapshot, no frame in
time) is skipped, not retried.

The preview is:

- a raw **H.265 Annex-B elementary stream**, currently 480×270 (coded
  480×272), full camera field of view, upright;
- every frame an **IDR** carrying its own VPS/SPS/PPS, so any prefix that ends
  on a frame boundary decodes on its own;
- **untimed**: frames are one interval apart on the device, but the stream
  carries no timestamps, so a player picks its own rate;
- about 3–10 KB a frame: roughly 0.5–2 MB for 30 minutes of recording at the
  default interval;
- stored **unencrypted**, also on units that encrypt their recordings
  (`SetRecordingKey`);
- **not part of the recording's upload**: neither the per-file upload nor the
  archive upload (`archive_upload.md`) carries it, and it is not a pullable
  file (`recordings_pull.md`): `RecordingEntry.file_count`, `files` and
  `size_bytes` neither count nor list it;
- deleted with the recording (`DeleteRecording`).

## Discovery

`RecordingEntry.preview_bytes` (23) is the preview's size in bytes, on every
entry of `ListRecordings`. It is 0 when the session has none: recording preview
off, a recording shorter than one interval, or firmware that does not store
one. It grows while the session is being recorded. A host that keeps a copy of
a preview can compare sizes to tell whether its copy is still current.

## Downloading it

`GetRecordingPreview { session_key }` (Command 58) names the recording by its
id (`RecordingEntry.session_key`). The answer has `CaptureSnapshot`'s form
(`snapshots.md`):

- one `CommandResult.snapshot` descriptor: `snapshot_id` = the command's
  nonzero `command_id`, `total_bytes` = the whole preview, `width` and
  `height` its picture size, `camera_index` the primary camera, and
  `capture_pts_us` 0;
- a finite `SnapshotChunk` stream on `/<device>/snapshot`, sent only to the
  requesting connection, carrying the preview's bytes from offset 0 in
  1..1024-byte chunks (in the field named `jpeg`).

There is no partial read. A transfer that is interrupted (disconnect, abort,
deadline) is requested again from the start.

The `snapshots.md` rules apply, except for two bounds that a video needs:

- **Size.** `total_bytes` is at most 16 MiB (16777216) for a preview, against
  128 KiB for an image. A larger preview is refused `preview_too_large`.
- **Deadline.** A preview takes longer than an image to deliver: about a
  minute for 2 MB over BLE. The device bounds each wait for queue capacity to
  30 seconds, as for an image; a receiver bounds the time **between chunks**
  to 30 seconds, not the whole transfer.

As for an image: address the directly connected device, register reception
before sending, and expect one snapshot or preview in flight at a time. A
request from another connection meanwhile is `busy`; a new request on the
same connection replaces that connection's own transfer, so a host that
abandoned a download (left the screen, lost interest) simply asks again.
A host that meets `busy` can retry after a few seconds: another connection's
preview takes about a minute on BLE.

Downloading is **refused while the device is recording** (`busy_recording`,
the code `OpenRecordingFile` uses for the same reason): a minute of transfer
over BLE costs the device CPU a recording cannot spare. Ask again once the
recording has stopped. A preview the host already holds needs no device and
plays at any time.

Refusals: `invalid_request` (zero `command_id`, empty or malformed
`session_key`, empty or hub-relayed target), `no_sdcard`, `no_such_session`,
`no_preview` (the session has none), `busy`, `busy_recording` (the device is
recording), `read_failed`,
`preview_too_large`, `unsupported` (firmware without the command, or no
snapshot transfer on this connection).

## Playing it

The bytes are a complete `.h265` file. Android's MediaCodec, iOS's
VideoToolbox and FFmpeg decode it as-is; a container-based player needs it
wrapped first (for example into MP4, with a frame rate the host chooses). A
time-lapse rate of a few frames a second shows a 30 minute recording, about
180 frames at the default interval, in under a minute.
