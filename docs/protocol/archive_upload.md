# Uploading a recording as one archive file

How a host has a device upload one recorded session as a **single archive file** through a
one-file upload link it obtained elsewhere (for example from its own backend). Sections 1–5 are
normative. The host's backend and how it issues links are not part of this contract.

The design in one paragraph: **the host hands the device a link, the device does the rest.**
`UploadRecording` gives the device one recording id and one Azure Blob SAS URL scoped to one
blob. The device streams the session's files into that blob as a POSIX tar archive, block by
block, and resumes after any interruption by asking the storage service which blocks it already
holds. The host follows progress through `ListRecordings` and frees the card with
`DeleteRecording` once its backend has confirmed the archive.

## 1. The recording id

Every session carries `RecordingEntry.session_key`: 128 random bits as 32 lowercase hex, minted
when the recording starts and written into its `session.json` and MCAP metadata. Unlike the
session name it is unique across devices. `DeviceState.recording_session_key` names the session
being recorded, and `RecordingStop.session_key` the one that stopped. `ListRecordings` and
`DeleteRecording` accept `session_key` in place of `session_name`.

`DeviceState.archive_upload_supported` is the capability probe: a host checks it before offering
archive uploads.

## 2. Commands

- **`UploadRecording { session_key, url }`** queues the upload and returns at once. The device
  keeps the job, link included, on internal storage until the upload ends, so it continues across
  host disconnects, new recordings, network drops and reboots. Sending it again for the same
  session with a fresh link for the **same blob** replaces the stored link; the upload continues
  where it stopped. A link to the blob of an upload already `COMPLETED` makes the device check
  that blob again: one the backend has deleted is uploaded afresh, one still there ends the job
  `FAILED` with `last_error` `already_uploaded`, since sending the same bytes again cannot change
  it. The link must be `https`, name one blob (`sr=b`) and grant read and write
  (`sp` containing `r` and `w`): read lets the device list the blocks already uploaded, so a
  resume does not start over, and write is what Put Block and Put Block List need (create
  alone permits only a single-request Put Blob). Over BLE the command is accepted only inside the encrypted session.
  Refusals: `no_such_session`, `active_session`, `invalid_request`, `insecure_link`, `no_sdcard`,
  `busy`, `unsupported`.
- **`CancelUpload { session_key }`** stops the upload and forgets the link. Uploaded blocks stay
  with the storage service until it discards them.
- **`DeleteRecording`** answers `protected` while an archive upload of the session is queued or
  running. Once the upload is `COMPLETED`, encrypted (`VREC`) parts no longer refuse the delete as
  they otherwise do (recordings_pull.md): every part is in the blob. The device never deletes a
  session on its own after an archive upload.

## 3. Progress and states

`ListRecordings { session_key }` reports the upload in the session's `RecordingEntry`:

| `upload_state` | Meaning |
|---|---|
| `PENDING` | has a link; waiting (a recording is running, no network, no valid clock, or the storage service could not be verified). `last_error` says which. |
| `IN_FLIGHT` | blocks are being sent; `bytes_uploaded` of `bytes_total` (= `archive_bytes`). |
| `NEEDS_LINK` | the storage service rejected the link (expired or revoked). Send `UploadRecording` again with a fresh link for the same blob. |
| `COMPLETED` | the archive is committed and its size checked. The local copy is kept. |
| `FAILED` | a permanent error (`last_error`), for example the blob already exists and the link cannot overwrite it, or `already_uploaded` (see `UploadRecording`). `UploadRecording` queues it again. |

`upload_blob` names the blob and `archive_bytes` the archive's exact size. Once a file's bytes are
uploaded, its `RecordingFile.md5` (MD5 of the file's data as stored in the archive, 32 lowercase
hex) and `archive_offset` (where that data starts in the archive) are filled; a host forwards them
to its backend to verify the archive.

The device uploads only while it is not recording, has a network, and has a valid wall clock (a
host sends `SetTime` when it connects). It verifies the storage service's TLS certificate chain
and host name; a failed check sends nothing and is retried later (`last_error`
`tls_verify_failed`).

## 4. Archive format

The blob is an uncompressed POSIX tar (`ustar`), readable by any tar implementation:

```
<session_key>/<part>.mcap      512-byte header │ data │ zero padding to a 512-byte boundary
…                              (every MCAP part, in name order)
<session_key>/session.json     512-byte header │ data │ padding          (always last)
end-of-archive marker          1024 zero bytes
```

- Headers: mode `0644`, uid/gid 0, empty owner names, `mtime` = the file's modification time.
- MCAP parts are stored byte for byte as on the device (an encrypted part stays `VREC`
  ciphertext). The device reads them as stored and never decrypts, so it uploads an encrypted part
  whether or not it holds the recording key; decryption is the backend's.
- `session.json` is stored byte for byte as on the device too, so every file's `size` and `md5` in
  `ListRecordings` are those of its archive member.

The archive carries no index of its own. A host forwards each file's `archive_offset` and `size`
to its backend, which can then fetch one file with an HTTP range request; a reader with only the
blob walks the 512-byte tar headers.

## 5. Blocks

The archive is uploaded as an Azure **block blob**. Each tar header, each 1 MiB slice of a file's
data (the last one shorter), each padding run and the end marker is one block. Every `Put Block`
carries `Content-MD5`, which the service verifies. Block ids have one fixed length and encode the
member index, a version tag of the file (from its size and modification time) and the slice
number, so a device recomputes the same plan after a reboot and a file changed on the card gets
new blocks. The archive appears only when `Put Block List` commits the blocks in order; the device
then confirms the committed size with `Get Blob Properties`.
