# Ingest API — a customer server as the upload destination (DRAFT)

**Status:** proposal, 2026-10-07. Nothing here is built. Firmware, the app and the
provisioning tools still know only the five storage clouds of
[`storage-providers.md`](storage-providers.md).

This document specifies a **sixth kind of upload destination**: an HTTP API that
the customer runs, which receives recordings from the rig directly instead of the
rig writing objects into a bucket. Some customers want their own service in the
path — to validate, register and route recordings as they arrive, to keep
storage credentials off the device, or because their storage is not an object
store at all. This is the contract that server implements and the rig calls.

The rig is the **only client**. The server is written by whoever runs it (the
customer, or GILabs — §11), so this document is the whole interface: a server
built from it alone, with no access to firmware source, MUST interoperate.

Contents: [1 Design](#1-design) · [2 Selecting it](#2-selecting-the-destination) ·
[3 Auth](#3-authentication) · [4 Resources](#4-resources-and-keys) ·
[5 Endpoints](#5-endpoints) · [6 Flow](#6-the-upload-flow) ·
[7 Errors](#7-errors-and-what-the-rig-does) · [8 Server MUSTs](#8-server-requirements) ·
[9 Identity](#9-identity-dedup-and-attribution) · [10 Firmware](#10-firmware-changes) ·
[11 GILabs server](#11-gilabs-as-a-server) · [12 Open](#12-open-questions)

---

## 1. Design

### 1.1 What it is modelled on

The GILabs vendor ingest (`data-platform`: `/api/v1/ingest/*`, the `gi-ingest`
CLI, the DPS `vendor-batch-ingest` flow) is the reference. Four of its ideas
carry over unchanged:

| Kept from vendor ingest | Here |
|---|---|
| **An explicit commit with a manifest** triggers processing, not discovery of objects. The partial-upload race goes away by construction. | `POST …/commit` with the part list (§5.5) — replaces "`session.json` uploaded last" as the completion marker. |
| **The server owns policy and identity** — episode id, dedup, duration floor, attribution. The client sends facts. | The rig never derives an episode id; the commit response returns the server's (§9). |
| **The manifest's field names** (`device_id`, `session`, `recorded_at`, `parts[{name,bytes,sha256}]`) | Same names, same meaning, so one server can serve both. |
| **The content digest** `sha256("{name}:{sha256}\n" for each part)` | Returned by commit; the rig can check it (§5.5). |

And five things are deliberately different, each because the rig is an
unattended, memory-starved device on flaky Wi-Fi rather than a human at a laptop:

| Vendor ingest | Here | Why |
|---|---|---|
| The unit is a **batch** of many episodes, sealed all-or-nothing | The unit is **one session** | A rig finishes sessions one at a time across days; a batch holds finished footage hostage to unfinished footage, and one torn object fails the whole seal. |
| Bytes go to S3 under **STS session credentials** | Bytes go **to the API** (or optionally a URL it hands out — §12) | The rig has no STS, no multipart, no token refresh; and the point of this destination is that the customer's server is in the path. |
| A **human-minted, vendor-wide** `gik_` key | A **per-device** key the server binds to one serial (§3.4) | Revoking one lost rig must not stop the fleet; a key in a device's flash will leak eventually. |
| Seal checks **sizes only**; plaintext is never hashed server-side | Every part is **SHA-256-verified before it is acknowledged** (§8) | The rig deletes its local copy on acknowledgement. An ack for bytes nobody checked is a deletion of the only good copy. |
| Identity is `(device, session, date)`, and the date comes from a clock | Upload resources are keyed by **content**, not by time (§4) | Rigs record with dead RTCs; `session_00042-9` recurs across cards. |

### 1.2 Constraints the protocol is shaped by

From `visio-embedded` as of 7843452. A server author needs these to understand
the MUSTs in §8.

- **CPU and RAM:** one 1 GHz Cortex-A7 with no AES instructions; about 45 MB of
  usable RAM. The rig streams request bodies from file descriptors and caps a
  **response body at 64 KiB**.
- **HTTP:** libcurl 8.7.1 with OpenSSL 1.1, **HTTP/1.1 only**, one connection,
  one request at a time, at background Wi-Fi priority. It always sends
  `Content-Length`, never `Transfer-Encoding: chunked`.
- **TLS:** the rig ships **no CA bundle and does not verify server
  certificates** today. Auth (§3) is designed so that this leaks no secret and
  cannot trick the rig into deleting data — but fixing it (§10) is still
  recommended.
- **Clock:** no NTP, no GNSS; time comes only from the phone. The rig learns an
  offset from the response `Date:` header and re-signs once on a clock-skew
  rejection.
- **Parts:** MCAP files of ~256 MB (`rotate_mb=256`; the compiled default is
  2048), possibly VREC ciphertext, which the rig never decrypts.
- **Scheduling:** nothing uploads while recording. The rig uploads only on a
  Wi-Fi station link, and throttles to 1 MiB/s while a viewer is connected.
  Any state change cancels an in-flight request mid-body, so **interrupted
  requests are normal traffic, not errors**.

---

## 2. Selecting the destination

`SetStorage` carries the same seven fields as for every cloud. There is still no
provider field (`storage-providers.md` §1): the destination is named by the
endpoint URL itself, through its **scheme**.

| `SetStorage` field | Ingest API meaning |
|---|---|
| `endpoint_url` | `visio+https://<host>[:port][/base/path]` — the scheme marks it; the rig dials `https://<host>[:port][/base/path]`. `visio+http://` is accepted for a LAN server. |
| `access_key_id` | The device key id (§3.4). ≤ 63 bytes. |
| `secret_access_key` / `sealed` | The device key secret: 32 random bytes, base64url, no padding (43 chars, fits the plaintext field). Provisioning SHOULD send it sealed. |
| `bucket` | Optional **destination hint**, passed through verbatim (e.g. a project id). Empty is valid. |
| `region`, `prefix`, `status_prefix` | Unused. MUST be empty; a non-empty value fails `TestStorage` with a message that names the field. |

Provider detection in `storage-providers.md` §2 gains one row, matched **before**
the host rules:

| Match | Provider |
|---|---|
| scheme `visio+https` or `visio+http` | `IngestApi` |

**Why the scheme and not a host or path rule.** The host is arbitrary (it is the
customer's), so the host suffix rules cannot name it, and today it would fall
through to `AwsS3` and be signed as S3. A path rule ("has a path → API") is
ambiguous, because operators routinely paste path-style bucket URLs
(`https://s3.amazonaws.com/my-bucket`). The scheme is part of the URL the
operator typed, so it cannot disagree with it, which is the reason §1 forbids
a provider field.

---

## 3. Authentication

### 3.1 Request signing (`VISIO-HMAC-SHA256`)

Every request is signed with the device secret. **No bearer token is ever
sent.** Bearer tokens would leak to anyone between the rig and the server,
because the rig does not verify certificates (§1.2).

Headers on every request:

```
X-Visio-Date:           20261007T031522Z          (rig's corrected clock, UTC)
X-Visio-Content-SHA256: <64 lowercase hex>        (of the request body; of the empty string if none)
Authorization:          VISIO-HMAC-SHA256 Credential=<key_id>, Signature=<64 lowercase hex>
```

```
string_to_sign =
    "VISIO-HMAC-SHA256" "\n"
    X-Visio-Date        "\n"
    key_id              "\n"
    METHOD              "\n"          uppercase
    path                "\n"          as sent, percent-encoded, base path included
    canonical_query     "\n"          params sorted by name, k=v joined by &; empty if none
    content_range       "\n"          the Content-Range header value, or empty
    X-Visio-Content-SHA256

signature = hex( HMAC-SHA256( base64url_decode(secret), string_to_sign ) )
```

This is SigV4 with the headers fixed and nothing derived from a date. It reuses
the HMAC and SHA-256 code the rig already has. A server can implement it in
about 30 lines. Planned, not yet written: a reference implementation at
`python/visio_schema/ingest/sign.py`, pinned by
`tests/golden/ingest_sign_vectors.txt`. Both get written once this spec is
accepted.

**For a part body, `X-Visio-Content-SHA256` is the hash of the WHOLE part, not
of the chunk sent** (§5.3). The rig computes that hash once, before the first
byte goes out, so chunked or resumed sends need no extra hashing pass. The
signed `Content-Range` stops a chunk from being moved to a different offset.

### 3.2 Clock skew

The server MUST accept `X-Visio-Date` within **±15 minutes** of its own clock,
and MUST send a `Date:` header on **every** response, including errors. Outside
the window it answers `401` with `"code": "clock_skew"`. The rig then corrects
its offset from that response's `Date:` and re-signs **once**. This is the
behaviour the firmware already has for S3 (which answers 403), with the status
moved to 401 so that a skew is never confused with a permissions failure.

Replay inside the window is harmless by design: every write is idempotent (§4),
so a replayed request can only restate what the rig already said.

### 3.3 Receipts: the server signs what it stored

A `2xx` response is not enough to justify deleting the only copy of a
recording. Without certificate checks, anything on the path can return `201`.
So every acknowledgement that lets the rig delete data carries a receipt that
only a holder of the device secret can produce:

```
X-Visio-Receipt: hex( HMAC-SHA256( secret,
    "VISIO-RECEIPT" "\n" device_id "\n" session_key "\n" part_name "\n" sha256 "\n" bytes ) )
```

The rig MUST verify it before tombstoning or deleting a part. If the receipt is
missing or wrong, the rig treats the response as a retryable failure and keeps
the file. The commit response carries the same kind of receipt over
`"VISIO-COMMIT\n" device_id "\n" session_key "\n" content_digest`.

### 3.4 Device keys

How keys are minted and stored is the server's business. The protocol requires
only two things:

- A key MUST be bound to **one** `device_id` (the 16-hex OTP serial). A request
  whose path names a different device is `403 device_mismatch`. This makes a
  leaked key worth one rig, and revocation per rig.
- The server SHOULD let a key be rotated without losing the device's open
  sessions. Sessions are keyed by device and content (§4), not by key.

A fleet-wide shared key is permitted, for a customer who accepts that trade.
The server then simply skips the binding check. The protocol does not prevent
it, and §11 does not do it.

---

## 4. Resources and keys

```
/v1/devices/{device_id}                                         the rig
/v1/devices/{device_id}/sessions/{session_key}                  one recording session
/v1/devices/{device_id}/sessions/{session_key}/parts/{name}     one MCAP part
/v1/devices/{device_id}/sessions/{session_key}/commit           the completion marker
/v1/devices/{device_id}/status                                  health beats
```

- **`device_id`**: the rig's 16-hex OTP uid, the same value as `serial` in
  `visio.capture` and `device_id` in `session.json`. **Never the hostname.**
  `GILABS-<code8>` is derived from it and is not unique-enough to key on.
- **`session_key`**: the first 32 hex characters of **SHA-256 of the session's
  first part** (`ego_0000.mcap`, ciphertext if VREC). It is content-derived on
  purpose:
  - it does not depend on the clock, so dead-RTC sessions with the same
    `session_NNNNN-<start>` name on two cards get two keys;
  - it is deterministic, so a rig whose `/userdata` was wiped by a cable flash
    re-derives the same key and finds its half-finished session on the server;
  - it needs no firmware schema change, and the rig hashes each part anyway
    (§3.1).

  The human-readable directory name travels in the body as `session`.
- **`name`**: the part's file name, as on the card (`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`,
  the vendor-ingest rule). Parts are ordered by name.

Every write is **idempotent**. `PUT`ting a session or a part twice is safe, and
so is committing twice. The rig gets every guarantee it needs from retrying,
and holds no state the server cannot reconstruct.

---

## 5. Endpoints

All bodies are JSON (`Content-Type: application/json`) except part bodies
(`application/octet-stream`). All responses MUST be ≤ 64 KiB. Paths below are
relative to the base URL of §2.

### 5.1 `GET /v1/devices/{device_id}` — probe and capabilities

This is what `TestStorage` and the app's Test button call, and what the rig
calls once per upload run. It only reads, so the probe has no side effects.

```jsonc
200
{
  "protocol": "visio-ingest/1",
  "server": "acme-ingest/2.3.0",              // free text, logged by the rig
  "device_id": "e54a1de522f7e0f1",
  "accepting": true,                           // false → the rig uploads nothing, retries the probe later
  "max_request_bytes": 67108864,               // largest single request body; MUST be ≥ 8 MiB
  "max_part_bytes": 4294967296,                // largest part accepted; MUST be ≥ 2 GiB
  "status_accepted": true                      // whether §5.6 is implemented
}
```

### 5.2 `PUT /v1/devices/{device_id}/sessions/{session_key}` — open (or reopen) a session

The rig sends this when it starts on a finished session, and again after any
`404 session_unknown`. The body carries what the rig knows **before** it uploads
anything:

```jsonc
{
  "session": "session_00042-1787776719",       // directory name, verbatim
  "device_kind": "ego",
  "destination": "",                           // SetStorage.bucket, verbatim
  "capture": { /* the visio.capture metadata record, every key as recorded, string values */ },
  "parts": [ { "name": "ego_0000.mcap", "bytes": 268435456 }, ... ]   // every part on the card, in order
}
```

```jsonc
200 (existing) / 201 (new)
{
  "session_key": "8f1c…",
  "state": "open",                             // open | committed
  "parts": [                                   // what the server ALREADY has, verified
    { "name": "ego_0000.mcap", "state": "stored",  "bytes": 268435456, "sha256": "…" },
    { "name": "ego_0001.mcap", "state": "partial", "received": 134217728 }
  ]
}
```

A part not listed is absent. The rig skips `stored` parts whose `bytes` match,
and resumes `partial` ones from `received`. If the state is already
`committed`, the rig re-commits, collects the receipt, and moves on.

`capture` is sent raw: the rig neither interprets nor renames it. Its keys are
those of the MCAP `visio.capture` metadata record at the head of every part
(`serial`, `hostname`, `session_name`, `start_time_unix`, `fps`, `app_version`,
`kernel`, `task`, `capturer`, `location`, `operator_id`, `environment_id`,
`recording_key_fingerprint`, `client_unix_us`, `client_utc_offset_min`,
`latitude`, `longitude`, `camN_calibration`; built in `visio-embedded`
`src/record/session/session_meta.cpp`). No document specifies that record
yet. Until one does, a server MUST ignore keys it does not know, and MUST NOT
require any key other than `serial`.

### 5.3 `PUT …/parts/{name}` — upload a part, whole or in chunks

**Whole** (when `bytes ≤ max_request_bytes`):

```
PUT …/parts/ego_0000.mcap
Content-Length: 268435456
X-Visio-Content-SHA256: <sha256 of the part>
```

**Chunked or resumed** (larger parts, or resuming a `partial`): sequential
ranges, each ≤ `max_request_bytes`, each one a separate PUT carrying the
**whole-part** hash:

```
PUT …/parts/ego_0000.mcap
Content-Range: bytes 67108864-134217727/268435456
Content-Length: 67108864
X-Visio-Content-SHA256: <sha256 of the WHOLE part>
```

| Response | Meaning | Rig does |
|---|---|---|
| `202 {"received": N}` | Chunk stored; part not complete | Sends the next chunk, from `N` |
| `201` + `X-Visio-Receipt` | Final byte received, **whole-part SHA-256 verified**, stored durably | Verifies the receipt, tombstones, deletes locally if configured |
| `409 already_stored` + `X-Visio-Receipt` | Identical part (same name, bytes, sha256) is already stored | Same as 201: the existing "conflict means already uploaded" rule (`storage-providers.md`, Overwrite protection) |
| `416 {"received": N}` | Range does not start at the server's offset | Resumes from `N` |
| `422 part_mismatch` | A **different** part (other sha256 or size) is stored, or was partly received, under this name | Stops on this part, keeps the file, and reports it as failed. **Never** treated as already-uploaded. |
| `422 hash_mismatch` | All bytes arrived, but they do not hash to the declared sha256 | The server discards what it received. The rig re-sends from byte 0, at most twice, then marks the part failed. |

A range that starts at 0 always restarts the part, so a rig that has lost its
own offset can always recover. The server keeps a `partial` for at least **7
days** since its last chunk (§8).

### 5.4 `GET …/parts/{name}` — part state

Returns the same entry shape as `parts[]` in §5.2. It is optional for the rig,
which normally learns this from the open response; it is used to resume one
part after a `416` it cannot interpret.

### 5.5 `POST …/commit` — the session is complete

The rig sends this once every part has a verified receipt and the existing
`session_marker_ready` gate holds: the session is not being recorded, has been
quiet for 60 s, and has a sidecar that is not torn. The body is the vendor-ingest
manifest episode entry, plus the sidecar inline:

```jsonc
{
  "device_id": "e54a1de522f7e0f1",
  "session": "session_00042-1787776719",
  "recorded_at": 1787776719.69,                // start_time_unix as recorded; may be a boot-epoch value — §9
  "parts": [ { "name": "ego_0000.mcap", "bytes": 268435456, "sha256": "…" }, ... ],
  "sidecar": { /* session.json, parsed; null if absent or torn */ }
}
```

```jsonc
200 + X-Visio-Receipt (commit form, §3.3)
{
  "state": "committed",
  "content_digest": "3852d3f8…",               // sha256 over "{name}:{sha256}\n" in part order
  "episode_id": "39c6fd7e-…",                  // the server's identity; opaque to the rig, logged
  "disposition": "accepted"                    // accepted | duplicate — informational only (§9)
}
```

| Error | Meaning | Rig does |
|---|---|---|
| `422 incomplete {"missing": [names], "mismatched": [names]}` | The manifest names parts the server does not hold, or holds with a different hash or size | Re-opens (§5.2), uploads those parts, commits again |
| `422 manifest_invalid {"message": …}` | Malformed manifest | Marks the session failed until the next `SetStorage` |

Committing is idempotent. A second commit with the same manifest returns the
same 200 and the same receipt. A commit with a **different** manifest after a
successful one is `409 already_committed`, and the rig logs it and moves on.

After commit, the server owns the session. The rig keeps nothing but its
tombstones.

### 5.6 `POST /v1/devices/{device_id}/status` — health beat

The body is today's `gilabs.status_report/1` JSON, unchanged. The server
answers `204`. It is best-effort: as with the bucket path, a missed beat is
dropped, never queued, and a `404` (server does not implement it) disables
beats until the next probe. A server MAY respond with `200 {"next_interval_s": N}`
to slow the beats down.

---

## 6. The upload flow

```
probe     GET  /v1/devices/D                              ── once per run; accepting? limits?
for each finished session S on the card, oldest first:
  key     K = sha256(S/ego_0000.mcap)[:32]
  open    PUT  /v1/devices/D/sessions/K   {session, capture, parts[name,bytes]}
          ← which parts are stored / partial
  for each part P not stored:
          h = sha256(P)                                   ── one pass, paced as today
          PUT …/parts/P   (whole, or ranges from `received`)  X-Visio-Content-SHA256: h
          ← 201/409 + receipt → verify → tombstone → delete local (if configured)
  commit  POST …/sessions/K/commit  {device_id, session, recorded_at, parts[], sidecar}
          ← 200 + commit receipt → verify → mark the session done locally
```

The rig's existing machinery stays as it is: the persisted queue, tombstones,
backoff, the recording yield and the send-rate caps. The change is in two
places. The verbs underneath are new (§10), and the completion marker becomes a
`commit` call instead of a `session.json` PUT.

Parts of one session upload in order, but the server MUST NOT depend on that.
Sessions with no committed predecessor can be committed in any order.

---

## 7. Errors and what the rig does

Every error body:

```json
{ "error": { "code": "part_mismatch", "message": "human-readable, shown in the app", "retryable": false } }
```

`code` is what the rig acts on. It is matched against the table below, with the
HTTP status as the fallback for unknown codes. `message` is surfaced verbatim in
`RecordingEntry.last_error`, so it should name the fix.

| Status | Codes | Rig behaviour |
|---|---|---|
| 2xx | — | Success (receipt rules in §3.3) |
| 401 | `clock_skew` | Correct the clock offset from `Date:`, re-sign once |
| 401 | `bad_signature`, `unknown_key` | Stop the run. Report "credential rejected". Do not retry until the next `SetStorage`. |
| 403 | `device_mismatch`, `revoked`, `forbidden` | As 401 `unknown_key` |
| 404 | `session_unknown` | Re-open (§5.2) and continue |
| 404 | anything else, on the probe | "Not an ingest API at this URL" — `TestStorage` fails |
| 409 | `already_stored` | Success (§5.3) |
| 409 | `already_committed` | Log; the session is done |
| 413 | `too_large` | Re-probe for `max_request_bytes` and re-chunk; if the part exceeds `max_part_bytes`, mark it failed |
| 416 | — | Resume from `received` |
| 422 | `part_mismatch`, `manifest_invalid` | Mark that item failed and keep its files. **Not retried** until the next `SetStorage`. |
| 422 | `hash_mismatch` | Re-send from 0, at most twice |
| 422 | `incomplete` | Upload the missing parts, then re-commit |
| 429, 503 | any | Wait `Retry-After` (seconds, capped at 1 h), else back off as today. Does **not** count toward `kMaxAttempts`, because the server asked for it. |
| 507 | `quota_exceeded` | Pause uploads for 1 h; surface it in the app |
| other 4xx | — | Treated as 422: the item fails and its files are kept |
| other 5xx, network | — | Normal backoff (1/2/4/…/300 s), counted toward `kMaxAttempts` |

**No error ever causes a local delete.** Only a verified receipt does.

---

## 8. Server requirements

A conforming server:

1. **Acknowledges only what is durable and verified.** `201`/`409` on a part
   means the whole part is persisted wherever the server keeps it **and** its
   SHA-256 matched. A server that writes through to object storage MUST NOT
   answer before that write has succeeded. The rig is about to delete its copy.
2. **Hashes the bytes it received**, streaming, rather than trusting the header.
3. **Keeps partials ≥ 7 days** after their last chunk. A rig can be off Wi-Fi for
   a long weekend.
4. **Tolerates abandoned requests.** The rig cancels mid-body whenever its
   state changes. Bytes received before a cancel MAY be kept as a `partial`
   (good) or discarded (allowed: the rig resumes from whatever offset is
   reported).
5. **Allows slow requests.** 64 MiB at the rig's 1 MiB/s viewer cap is about a
   minute; whole 256 MiB parts at the 450 KiB/s floor take 10 minutes. Request
   timeouts SHOULD be ≥ 15 minutes, or `max_request_bytes` set to fit.
   Reverse proxies cap bodies too. Cloudflare's proxy, for example, refuses
   bodies over 100 MB on its lower plans, so set `max_request_bytes` below
   whatever sits in front of you.
6. **Answers `Expect: 100-continue`.** curl sends it on large PUTs. The server
   should reject a bad signature or `413` **before** the body is transmitted,
   so the rig does not spend Wi-Fi time on a doomed upload.
7. **Sends `Date:` on every response** (§3.2), and keeps every response body
   ≤ 64 KiB.
8. **Is idempotent** on every write (§4).
9. **Never requires the rig to decide policy.** Duplicates, short sessions,
   unattributed sessions and dead-clock dates are all committed and then
   handled server-side (§9). A rule that refuses them at the edge loses
   footage that cannot be re-recorded.

---

## 9. Identity, dedup and attribution

These are the **server's** decisions. The protocol gives it the raw material
and asks nothing back. This is the vendor-ingest rule that the platform owns
policy and the client sends bytes:

- **Episode id.** Derived by the server from whatever it likes; returned by
  commit for the rig's log. The rig never computes one. For a GILabs-compatible
  id, see §11.
- **Dedup.** `content_digest` identifies identical content across devices,
  sessions and re-deliveries. A commit of content the server already has
  returns `200 disposition: duplicate` — never an error, because the rig's
  correct action (mark done) is the same either way.
- **Recording time.** `recorded_at` and `capture.start_time_unix` are what the
  rig's clock said, and can be boot-epoch values. A value below 1e9 is not a
  date. The server SHOULD record the commit time and flag the clock as suspect,
  rather than storing 1970 or 2021 as the capture date.
- **Attribution.** `operator_id` / `environment_id` come from `capture` (or
  `sidecar`), stamped on the rig by the recording-setup QR. A server MAY accept
  sessions without them and attribute them later. It SHOULD NOT reject them,
  for the reason in §8.9.
- **Encrypted parts.** VREC parts arrive as ciphertext and are hashed as
  ciphertext. `capture.recording_key_fingerprint` tells a server holding the
  key which key to use.

---

## 10. Firmware changes

Everything above the transport stays as it is: `S3Uploader`'s queue, tombstones,
budget and backoff. The required changes are:

1. **Provider row `IngestApi`**, detected by scheme (§2), dispatched before the
   host rules. `StorageConfig::valid()` stops requiring `region` and `bucket` for
   this row, and requires `prefix` and `status_prefix` to be empty.
2. **A transport interface.** Pull `s3_put_file` / `s3_put_buffer` /
   `s3_list_bucket` behind one interface (`put_part`, `put_status`, `probe`), and
   add `commit_session`, a no-op on bucket rows. The bucket rows keep uploading
   `session.json` as their marker. Of the two options the firmware review found,
   this is preferable to a sixth row in `StorageProviderSpec`, because the API
   row has verbs the bucket rows do not (open, commit, ranges).
3. **The `VISIO-HMAC-SHA256` signer** (§3.1) beside `sigv4.cpp` and
   `azure_sign.cpp`, pinned by the golden vectors.
4. **Range resume.** Stream `[offset, offset+len)` from the fd. The read
   callback already streams. The resume offset comes from the open response,
   so no new persisted state is needed.
5. **Receipt verification** before tombstoning (§3.3), on this row only.
6. **Commit replaces the `session.json` PUT** for this row. The sidecar goes
   inline.
7. **Recommended, independent of this spec:** ship a CA bundle (or pin the
   server's key) and turn on `CURLOPT_SSL_VERIFYPEER`. Receipts stop a MITM
   from causing a delete, but cannot stop it from **reading** unencrypted
   recordings.

Companion app and provisioning tools: add the row and the scheme to the shared
`PROVIDERS` table, using the vocabulary rule in `storage-providers.md` §1.1:
**Endpoint**, **Device key ID**, **Device key secret**, **Destination
(optional)**. The rig is still listed by the app from the device, over the
bus; there is no list call on this API (§12).

---

## 11. GILabs as a server

The GILabs backend can implement this same protocol, so a vendor rig can
upload to GILabs with no SD-card offload and no CLI. Mapped onto what exists in
`data-platform` and DPS:

| Protocol | GILabs implementation |
|---|---|
| Device key | A new `vendor_device_keys` row: `(vendor_id, device_id, key_id, secret_hash, revoked_at)`, minted in the vendor portal per rig, delivered by the existing recording-setup QR (sealed). Distinct from `gik_` keys, which stay human and vendor-wide. |
| Open session | Upserts an ingest record keyed `(vendor, device_id, session_key)`. Parts land in R2/S3 under `v1/<slug>/rig/<device_id>/<session_key>/` (the batch layout, with a device folder in place of the batch). |
| Part PUT | Streams to multipart storage in the server, hashing as it goes. The rig never sees a storage credential. Per-part CRC32 to R2 (R2 rejects per-part SHA-256), with the whole-part SHA-256 computed in the server. |
| Commit | Builds the canonical manifest exactly as `seal_batch` does, as a **one-episode batch**, and hands it to `vendor-batch-ingest` unchanged. `register_episode` then derives `episode_id` with `episode_identity.derive_episode_id(date, device_id, session)` and the digest discriminator, and dedups by `content_digest`. Identity stays one implementation in each repo. |
| `destination` | A `project_id` the vendor holds a `vendor_projects` grant for; empty means the vendor's default grant, as `POST /ingest/batches` does today. |
| Status beats | `device_status`, keyed by the vendor's device. This gives the fleet dashboard a server-side source. |

Doing this needs a DDL review first: `vendor_device_keys`, and `ingest_batches`
gaining a `device_id` / `session_key` or a sibling table. Per the vendor-ingest
design, the episode rows themselves need no new link table: `batch_id` on
`episodes` already marks reported attribution as authoritative.

---

## 12. Open questions

1. **Delegated upload (presigned part URLs).** A server that wants the bytes to
   go straight to its storage could answer the open call with a per-part
   `upload_url` + headers. The rig would PUT there and let the commit verify,
   like the vendor CLI's seal. Deferred from v1. It reintroduces the
   per-provider overwrite guards and checksum quirks this destination exists to
   avoid, and nobody has asked for it. If added, it is a capability in the
   probe response, and v1 rigs ignore it.
2. **Listing.** The app's cloud recordings screen lists the bucket. An API
   destination has no bucket to list. Options: a `GET /v1/devices/{id}/sessions`
   (adds a read scope to a write-only key), or the app shows device-side state
   only. Leaning to the latter.
3. **`session_key` vs a recorded session UUID.** A random UUID minted at
   `StartRecording` and written into `visio.capture` would key sessions without
   a hash pass, but only for recordings made after that firmware ships. The
   content key works for every card already in the field, so it is the v1
   choice. A UUID can be added later as metadata.
4. **Range resume vs whole-part retry.** Whole-part retry would be enough for
   the firmware as it is today, since it re-sends a part from byte 0. Ranges are
   in v1 for two reasons: proxies cap request bodies (§8.5), and re-sending
   256 MB over hotel Wi-Fi for every dropped connection is the dominant cost of
   flaky links. If customer servers find ranges burdensome, a
   `max_request_bytes ≥ max_part_bytes` server never sees one.
5. **Delete after commit vs after part.** The rig deletes a part on its own
   receipt (today's behaviour, now with verification). A stricter mode could
   keep all parts until the commit receipt. That costs card space on long
   sessions but protects against a server that loses partially committed
   sessions. It could be a server-advertised flag in the probe.
