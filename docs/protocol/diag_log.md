# Visio Diagnostic Log — line format, containers, files

A Visio device keeps a bounded, encrypted ring of its own log so a unit that
comes back carries weeks of history rather than the ten seconds an RMA
self-check can see. This document is the **wire contract** for that log: the
shape of one line, the containers it is stored and shipped in, and the names
of the files. Firmware writes it; `visio_schema.diag` (Python) and the
`visio-diag` tool read it; the golden vectors in `tests/golden/` pin the
crypto from both sides.

This document is canonical. Implementations MUST conform.

## 1. One line

Every record is one line of UTF-8 text, terminated by `\n`, of the form

```
YYMMDD HH:MM:SS.mmm +SSSSSSS.mmm S [TAG] message k=v k=v
260912 03:14:15.926 +0001234.567 I [COLLECTOR] imu: 474 fps  cam0: 30 fps (62.0 C)
------ --:--:--.--- +0000041.002 W [WIFI] scan found 0 networks
```

The first four whitespace-separated fields are the **stamp**; everything after
the fourth field is the **text**.

| # | Field | Form | Meaning |
|---|---|---|---|
| 1 | date | `YYMMDD`, UTC | Wall-clock date. `------` when the wall clock has never been set this boot. |
| 2 | time | `HH:MM:SS.mmm`, UTC | Wall-clock time. `--:--:--.---` under the same condition. Never a faked 1970 value. |
| 3 | mono | `+` then seconds since boot with 3 decimals, zero-padded to 7 integer digits | `CLOCK_MONOTONIC`. Always valid. A **decrease** from one line to the next is how a reader finds a reboot boundary with no other bookkeeping. |
| 4 | severity | one character | `D` debug, `I` info, `W` warning, `E` error. Other letters are reserved. |
| 5… | text | free | Conventionally begins with a bracketed tag naming the subsystem — `[COLLECTOR]`, `[WIFI]`, `[OTA]` — followed by a message. `key=value` pairs anywhere in the text are conventional, not required, and a reader that does not know a key ignores it. |

Rules a writer MUST follow and a reader MAY rely on:

- **The stamp is applied where the line is produced**, not where it is stored
  or transmitted. Every copy of a line — console, tmpfs log, flash ring, card
  ring, bus — carries the same bytes.
- **Fields are split on whitespace, never on byte columns.** The fixed widths
  above are for eyeballing; a monotonic clock past 9,999,999 s simply widens
  its field and every parser keeps working.
- The date and time fields are both dashes or both set. The dashed form means
  *unknown*, and readers MUST NOT substitute a guess: the device may have been
  running for weeks under a clock it was never given.
- A line contains no `\n` and no `\r`. A writer that receives an embedded
  newline splits it into two records.
- Year is `2000 + YY`. This format has a Y2.1K problem and accepts it.

### Reserved tags

| Tag | Written by | Meaning |
|---|---|---|
| `[BOOT]` | the daemon, first record of every run | `label=GILABS-… uid=… fw=… slot=a boot_seq=N` — identifies the unit and the run. A ring file is named after the unit, but a name is the one piece of metadata a human can destroy; this line is the belt to that braces. |
| `[DEATH]` | the daemon, at start | Lines recovered from the supervisor's `last_death.log` — the tail of the previous run that ended without a clean exit. Already stamped; ingested verbatim. |
| `[KERN]` | the daemon, from `/dev/kmsg` | The kernel ring. mmc/SD I/O errors, USB resets, OOM kills, thermal trips. The stamp is the daemon's; the kernel's own monotonic microseconds follow in the text. |
| `[INIT]` | the daemon, at start | The init chain's output (`S50sdcard`, `S36boot_confirm`, `S37watchdog`), which runs before the daemon exists. |

## 2. The containers

Two containers, both the recording container (`VREC`, see
`cpp/include/visio_schema/mcap/recording_crypto.hpp`) under a different
`CipherSuite`: a 32-byte plaintext header, then ChaCha20 ciphertext with the
plaintext-offset identity — ciphertext offset `P + 32` is plaintext offset
`P`. Nothing about the cipher, the key derivation or the header layout differs
from `VREC` except the four magic bytes and the derivation label:

| Suite | Magic | Key label | Used for |
|---|---|---|---|
| `VDLG` | `"VDLG"` | `visio-diag-v1` | ring files on flash and on the SD card |
| `VDLW` | `"VDLW"` | `visio-diag-wire-v1` | one batch of lines on the bus (`DiagLog.blob`) |

```
 0  magic       (4)   "VDLG" | "VDLW"
 4  format      u8    1
 5  cipher      u8    1 (ChaCha20)
 6  reserved    u16   0
 8  key_fp      (8)   SHA-256(diag_key)[:8]
16  nonce       (12)  CSPRNG, fresh per file / per message, used exactly once
28  bytes_valid u32 LE  plaintext bytes the writer last recorded as valid; 0 = read to EOF
32  ciphertext  …

file_key = HMAC-SHA256(diag_key, key_label || nonce)
```

**The key is a fleet key**, 32 raw bytes staged into the image at
`/etc/visio_diag.key` exactly as the OTA bundle key is. It is not the
recording key, it is not per-unit, and the recording keyring never holds it.
A device with no key staged writes the ring **in the clear** — a plaintext
`.vdlg` starts with the line format above, not with a magic — and says so at
boot. A reader accepts both.

**Two suites, not one, because both share that key.** The ring writer and the
bus publisher are independent producers. Under one label they would draw from
a single nonce space and have to coordinate counters across two subsystems
forever; a reused nonce under a stream cipher recovers both plaintexts from
the two ciphertexts alone. Separate labels make them different keystreams by
construction. `tests/golden/diag_vectors.txt` pins this: it reuses `VREC`'s
key, nonce and plaintext byte for byte, so the only variable is the suite.

**`bytes_valid` is the tear detector.** A ring file is appended to across
reboots and never rewritten, so the bytes past a power-cut tear are keystream
over stale data — uniform noise a stream cipher will never flag. The writer
rewrites `bytes_valid` in place after every flush; the reader stops there. A
tear between the data write and the header update costs one flush, which is
the intended granularity. `0` keeps `VREC`'s read-to-EOF behaviour and is what
every recording has always written.

**A `VDLW` message is a complete container**, header included. A reader may
join mid-stream, a stream policy may drop messages, an MCAP may be torn — any
of those makes a continuous keystream unrecoverable from that point on, so
every message decrypts alone.

## 3. Files

```
/userdata/umi_embedded/diag/GILABS-<code8>.flash.<seq>.vdlg   flash ring, W/E lines only
/userdata/umi_embedded/vitals.txt                          plaintext lifetime counters
/mnt/sdcard/GILABS-<code8>.<seq>.vdlg                      card ring, everything
/mnt/sdcard/GILABS-<code8>.vitals.txt                      plaintext, the signpost
```

- **Two tiers, two name shapes.** The flash tier carries `.flash` so a
  catalogue holding both tiers never lists one name twice — the read service
  resolves a request by name alone. The card tier keeps the bare label: it is
  the file a customer sees at the card root.
- **Named after the device.** `GILABS-<code8>` is the label printed on the
  unit and the key PLM joins on. A `.vdlg` leaves the device by being copied
  off a card into an email, stripped of all context; the name is the only
  metadata that survives the copy. The header's `key_fp` is fleet-wide and
  identifies nothing.
- **The card files sit at the card root**, beside `data/`, so they are the
  first thing in the window when a customer puts the card in a laptop — the
  delivery path for a unit that will not boot.
- **`<seq>` is a monotonically increasing integer.** The writer appends to the
  newest file while it has room, creates the next when it is full, and
  `unlink`s the oldest when the count would exceed the ring size. A file is
  never truncated and reused: a new file is a new inode and a new nonce.
- **`vitals.txt`** is `key=value` lines, plaintext, never encrypted: a dozen
  lifetime counters (`boots`, `watchdog_resets`, `powered_hours`,
  `recorded_hours`, `sessions`, `cards_seen`, `files_rotated`, `diag.dropped`)
  that survive the ring rolling. It opens with two comment lines saying what
  the `.vdlg` files are and where to send them. It is the only file in this
  design a customer is meant to open.
- Everything under `/mnt/sdcard` is exported read-plus-delete over MTP; the
  firmware's delete guard refuses these names.

## 4. Getting it off the device

Three paths, and the decoder is the same for all of them:

1. **Bus** — `CONTROL_STREAM_DIAG` (`service/diag/diag.proto`): `DiagList`,
   then `DiagRead` chunks of the raw file. The host is a blind relay.
   `visio_schema.wire.diag.list_files` / `read_file` drive it over any
   `send`/`recv` pair, the way `wire.ota.relay` drives an update; a gap in the
   chunk stream aborts the read rather than splicing ciphertext.
2. **Recording** — `/<root>/diag_log` (`sensor/diag_log.proto`): one `VDLW`
   container per message, inside every MCAP the device writes.
3. **Card** — copy the files.

### A hub serves its leaves' logs

A client reaches one device, and on a rig that device is the hub. So a hub's
`DiagListing` carries every attached leaf's files after its own, under the
names the leaf gave them — which already start with the leaf's label (§3), so a
pulled set says whose each file is. A `DiagRead` of a leaf's file is served
by the hub reading it from that leaf and answering on its own `/<root>/diag`
under the requester's session. The client does nothing different.

Three rules make that work, and a peer implementing either end MUST follow
them:

- **A leaf ignores a request addressed to another device.** A hub relays an
  addressed `DiagRequest` down every leaf link; without this rule every leaf
  serves every request.
- **Session ids with the top bit set belong to a hub's own requests** to its
  leaves. A hub consumes the replies to them instead of relaying them upward,
  where they would reach the host twice and land in the recording. Hosts pick
  session ids below 2^63.
- **A leaf that does not list in time is left out** of the hub's listing rather
  than failing it; its timeouts sit under the host's stall timer
  (`visio_schema.wire.diag.STALL_TIMEOUT_S`). A leaf that fails mid-read fails
  that read, with the leaf's own error code.

## 5. Reading

```python
from visio_schema.diag import iter_records, split_boots
for rec in iter_records("GILABS-7K3M9QP2.3.vdlg", key=diag_key):
    ...
```

`visio_schema.diag.parse_line` is the reference parser for §1;
`iter_records` opens a `.vdlg` (or a plaintext one) and yields a record per
line, **including lines it could not parse** (severity `?`), so a reader is
never handed a log that quietly lost lines. `visio-diag open|render|counters`
is the command-line tool. Neither is part of the top-level `visio_schema`
facade.
