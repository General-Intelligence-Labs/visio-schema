# Recording preview

Before a recording is uploaded, the wearer may need to see what it holds and
agree to the upload. A recording is far too large to move over Bluetooth, so a
device can also keep a **preview** of each recording: a short, low-resolution
video, one frame every few seconds, that a host downloads in seconds over any
link, BLE included, and plays back as a time-lapse.

This document specifies the preview, how a host finds it, the command that
downloads it, and how to play it. The transfer is the one snapshots use
([`snapshots.md`](snapshots.md)).

## 1. What the preview is

| | |
|---|---|
| Content | the device's primary camera, full field of view, upright |
| Frame rate | one frame every few seconds of recording (10 s by default) |
| Picture | 480 × 270, coded as 480 × 272 (the bottom two rows are black) |
| Format | a raw **H.265 Annex-B elementary stream**; every frame is an **IDR** carrying its own VPS/SPS/PPS |
| Size | about 3–10 KB a frame: roughly **0.5–2 MB for 30 minutes** of recording at the default rate |
| Timing | none: frames are one interval apart on the device, but the stream carries no timestamps |
| Encryption | none, also on units that encrypt their recordings (`SetRecordingKey`) |

Because every frame carries its own parameter sets, any prefix of the stream
that ends on a frame boundary decodes on its own.

The preview is **not part of the recording**:

- neither the per-file upload nor the archive upload
  ([`archive_upload.md`](archive_upload.md)) carries it;
- it is not a pullable file ([`recordings_pull.md`](recordings_pull.md)):
  `RecordingEntry.file_count`, `files` and `size_bytes` neither count nor list
  it;
- it is deleted with the recording (`DeleteRecording`).

## 2. Finding a preview

`RecordingEntry.preview_bytes` (23), on every entry of `ListRecordings`, is the
preview's size in bytes:

- **0** means the recording has none: the device does not keep previews, the
  recording is shorter than one interval, or the firmware predates previews.
- A non-zero value grows while the recording is still running. A host that
  keeps a copy of a preview can compare sizes to tell whether its copy is
  current.

## 3. Downloading a preview

`GetRecordingPreview { session_key }` (Command 58) downloads one recording's
whole preview, naming the recording by its id (`RecordingEntry.session_key`).

The answer has `CaptureSnapshot`'s form ([`snapshots.md`](snapshots.md)):

1. One `CommandResult.snapshot` descriptor: `snapshot_id` is the command's
   nonzero `command_id`, `total_bytes` the whole preview, `width` and `height`
   its picture size, `camera_index` the primary camera, and `capture_pts_us`
   0.
2. A finite `SnapshotChunk` stream on `/<device>/snapshot`, sent only to the
   requesting connection, carrying the preview's bytes from offset 0 in
   1..1024-byte chunks (in the field named `jpeg`). Register reception before
   sending the command: chunks can arrive before the descriptor.

Address the directly connected device, as for `CaptureSnapshot`.

| Rule | |
|---|---|
| **Not while recording** | the device refuses with `busy_recording` (the code `OpenRecordingFile` uses for the same reason): a minute of transfer over BLE costs the device CPU a recording cannot spare. Ask again once the recording has stopped |
| Whole file only | there is no partial read; a transfer that is interrupted (disconnect, abort, deadline) is requested again from the start |
| Size | `total_bytes` is at most 16 MiB (16777216) for a preview, against 128 KiB for an image; a larger preview is refused `preview_too_large` |
| Deadline | the device bounds each wait for queue capacity to 30 seconds, as for an image. A receiver bounds the time **between chunks** to 30 seconds, not the whole transfer: a preview takes longer than an image to deliver |
| One transfer at a time | a request from another connection while a snapshot or preview is in flight is `busy`; retry after a few seconds. A new request on the **same** connection replaces that connection's own transfer, so a host that abandoned a download simply asks again |

Refusals: `invalid_request` (zero `command_id`, empty or malformed
`session_key`, empty or hub-relayed target), `no_sdcard`, `no_such_session`,
`no_preview` (the recording has none), `busy`, `busy_recording` (the device is
recording), `read_failed`, `preview_too_large`, `unsupported` (firmware
without the command, or no snapshot transfer on this connection).

**How long it takes.** Over BLE, the preview of a one-hour recording (about
0.9 MB) arrives in about 20 seconds; over USB or Wi-Fi, in about a second.

## 4. Playing a preview

The bytes are a complete `.h265` file. A host either decodes it directly or
wraps it in a container first.

- **Decode directly.** Android's MediaCodec, iOS's VideoToolbox and FFmpeg
  decode the stream as-is, one frame at a time.
- **Wrap it in an MP4.** Most platform video players open MP4, not a raw H.265
  stream. Wrapping needs no decoding or re-encoding: each frame (access unit)
  becomes one MP4 sample, with its NAL units length-prefixed instead of
  start-code delimited, and every sample is a sync sample. Because the
  parameter sets are the same in every frame, they can go once into the sample
  description (`hvc1` with an `hvcC` box) and be dropped from the samples; if
  they ever differ, keep them in each sample and use `hev1`. Write `moov`
  before `mdat` so playback starts without reading the whole file.

The stream carries no timing, so the host picks a time-lapse rate. At 4
frames a second, 30 minutes of recording at the default rate, about 180
frames, plays in 45 seconds.
