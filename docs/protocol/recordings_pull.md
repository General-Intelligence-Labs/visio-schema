# Pulling recordings off a device

How a host lists the recorded sessions on a device's local storage and copies their files off it.
Sections 1–7 are normative; section 8 is tuning, which implementations may choose differently.
`visio_schema.wire.recordings` (Python) is the reference client, with twins in
`cpp/include/visio_schema/wire/recordings.hpp` and `ts/src/wire/recordings.ts`;
`tests/golden/recordings_wire_vectors.txt` pins their codecs to each other.

The design in one paragraph: **control rides the existing Command / CommandResult pair; the file
bytes ride a bare TCP socket.** A host lists with `ListRecordings`, asks for one file with
`OpenRecordingFile`, then connects to the port the answer names and reads until the device closes.
TCP supplies order, delivery and flow control, so the socket carries nothing but file bytes.

## 1. Listing

`ListRecordings` (Command body 18) answers with `CommandResult.recordings`, a `RecordingsList`.

- **Unchanged call.** With `cursor` and `session_name` both empty, the device returns the newest
  `limit` sessions (0 = device default) without files — exactly the reply a host written before
  this document received. The new fields in the reply are additive.
- **Paging.** When `next_cursor` is non-empty, more sessions exist; the host passes it back verbatim
  as `cursor`. A cursor is opaque, at most 63 bytes, and names a position that stays valid when
  sessions are added or removed between pages. A host MUST stop if a cursor repeats.
- **One session's files.** With `session_name` set, the reply holds exactly that session, with
  `files` filled in. An unknown session answers `ok=false`, `error_code="no_such_session"`.
- **Which files.** `files` lists the session's MCAP parts and its `session.json`, in name order —
  the files that make a complete copy of the session. Upload tombstones, temporary and repair files
  are not listed. `file_count` is always set.
- **`writing`.** A file the device has open for write is listed with `writing=true` and is not
  pullable: the part currently being recorded, and the active session's `session.json` (rewritten
  when the recording stops). Every other file of an active session is a finished file.
- **`complete`** is true for an MCAP part carrying its end magic and for `session.json`;
  **`encrypted`** is true for a `VREC` part. An encrypted part is pulled as ciphertext.
- **`read_while_recording`** reports the device's policy (section 6).

Listing is allowed on every link and while recording.

## 2. Opening a file

`OpenRecordingFile` (Command body 43) answers with `CommandResult.file_open`, a `RecordingFileOpen`.

The device checks, and refuses with the first failing code:

1. the link: USB-NCM only — `forbidden_link` on Wi-Fi, `no_data_link` on a link with no IP path
   (USB serial);
2. storage present — `no_sdcard`;
3. names — `invalid_request` for a name that is empty, over 63 bytes, contains `/` or NUL, or
   starts with `.`;
4. existence — `no_such_session`, `no_such_file`;
5. `writing` — the file is open for write;
6. recording policy — `busy_recording` (section 6);
7. other work — `busy` when another host is pulling, or an OTA, format or storage recovery is in
   progress;
8. identity — when `expect_size` or `expect_mtime_ns` is non-zero, both must equal the file's
   current values, or `changed`; a part being repaired also answers `changed`;
9. range — `out_of_range` when `offset` exceeds the file size.

On success the answer carries `port`, `offset` (equal to the request's), `length` (`file_size −
offset`, possibly 0), `file_size` and `mtime_ns`.

An accepted open is a **lease**: it belongs to the requesting host's network address, is consumed
by that host's next connection to `port`, and lapses unused after 10 seconds. A new open from the
same host replaces its unused lease and ends its transfer in progress, if any.

## 3. The data socket

The device listens on `port` on its USB-NCM address only.

- The host connects, reads, and **never writes**.
- On accept, the device looks for a live lease belonging to the peer's address. With none, it
  closes at once. With one, it consumes it, sends exactly `length` bytes of the file starting at
  `offset`, shuts down its sending side and closes.
- The device MAY close early — the recording policy requires it, a newer open from the same host
  replaced the transfer, a storage error occurred, or it is shutting down. It sends no status: an
  early close is visible to the host as fewer than `length` bytes, and the reason, if any, is the
  error code of the host's next open.

Because the host never writes, the device never closes with unread input, and a close cannot reset
away bytes still in flight.

## 4. Host rules

- **Success** is exactly `length` bytes followed by the device's close. Any fewer is a short read,
  whatever the cause (refused connection, reset, stall, early close).
- **Resume** after a short read by opening again with `offset` = bytes now held, and
  `expect_size`/`expect_mtime_ns` set to the first open's `file_size`/`mtime_ns`. A host MUST NOT
  join bytes from two opens whose identity differs; `changed` means start the file again.
- A host MUST keep reading its bus link while bytes arrive on the data socket. A transfer can
  run for minutes, and the device aborts a bus connection whose receive window stays closed for
  10 seconds (`TCP_USER_TIMEOUT`) — which is what a single-threaded host that only reads the data
  socket produces, even with streaming paused.
- A host SHOULD give up after several consecutive opens that deliver no bytes.
- A host SHOULD keep a partial file under a temporary name with its identity beside it, and give it
  its final name only when its size equals `file_size`.

## 5. Deleting a session

`DeleteRecording` (Command body 44) deletes one whole session. `Command.target_device` MUST name
the unit; a broadcast delete answers `invalid_request`. Refusals: `forbidden_link`, `no_sdcard`,
`no_such_session`, `busy_recording`, `active_session`, `busy`, `protected` (encrypted parts,
diagnostic files, or an upload of the session in progress), `delete_failed`. A session is deleted
whole or not at all as far as any listing can see.

## 6. Recording policy

`RecordingsList.read_while_recording` says whether a device serves pulls while it records.

- **false:** `OpenRecordingFile` answers `busy_recording` during a recording, and a recording that
  starts mid-transfer closes the transfer.
- **true:** finished files — including the finished parts of the session being recorded — may be
  pulled during a recording. Files with `writing=true` never are.

Deletes are refused during a recording either way.

## 7. The link during a pull

A host SHOULD turn off bulk streaming on its own bus link for the duration of a pull and MUST
restore it on every exit, exactly as `ota.md` §6 requires for an update. The rules are
`SetStreamPolicy` drops of `**/camera/*`, `**/imu/*/raw`, `**/imu/*/quat` and `**/audio/*`
(`PULL_QUIESCE_RULES` in each client). Recording to the device's own storage is not
affected by a link policy.

## 8. Tuning (not normative)

| | Value |
|---|---|
| File sender port | 50002 |
| Unused lease lifetime | 10 s |
| Host stall timeout (no byte) | 10 s |
| Host command timeout | 8 s |
| Opens without progress before giving up | 5 |
| Host receive buffer | 1 MiB |

## Error codes

| Code | Meaning |
|---|---|
| `busy_recording` | Refused because the device is recording (policy, or any delete) |
| `writing` | The file is open for write |
| `active_session` | Delete of the session being recorded |
| `busy` | Another host is pulling, or OTA / format / storage recovery is running |
| `forbidden_link` | Not allowed on this link (Wi-Fi) |
| `no_data_link` | This link has no IP path for the data socket (USB serial) |
| `no_sdcard` | No usable storage |
| `no_such_session`, `no_such_file` | Not there (deleted, uploaded and removed, or a stale listing) |
| `changed` | The file differs from `expect_size`/`expect_mtime_ns`, or is being repaired |
| `out_of_range` | `offset` beyond the file size |
| `protected` | Delete refused: encrypted parts, diagnostic files, or an upload in progress |
| `delete_failed` | The delete did not complete |
| `invalid_request` | Malformed names, or a delete without a target device |
| `unsupported` | The device does not implement the command |
