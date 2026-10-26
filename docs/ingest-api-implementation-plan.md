# Plan: GILabs Backend Support for the Rig Ingest API

**Status:** built 2026-10-22 and open as draft PRs, not merged or deployed; see
[`ingest-api-implementation-status.md`](ingest-api-implementation-status.md) for
what was built, every choice made along the way, and the open questions. The
migration was written ahead of the §6 DDL sign-off, for review.

**Scope:** make the GILabs backend (`data-platform`) and data-processing-services
(DPS) a server for the rig Ingest API, so a vendor's rig uploads its recordings
straight into our ingest bucket, with no SD-card offload and no CLI, and **only
rigs we have approved can upload**. The protocol itself is specified in
[`protocol/ingest-api.md`](protocol/ingest-api.md). Rig firmware work is listed
as a dependency (§8), not planned here.

**Basis:** a gap analysis of `data-platform` origin/main `f42918ed` (2026-10-20)
and DPS origin/main `9694ef74` (2026-10-19). Paths: `BE/` is
`data-platform/backend/app/`, `DPS/` is `data-processing-services/service/`.

## Contents

- [1. Summary](#1-summary)
- [2. What exists today](#2-what-exists-today)
- [3. Blockers](#3-blockers)
- [4. Design decisions](#4-design-decisions)
- [5. Phases](#5-phases)
- [6. Proposed schema changes (for sign-off)](#6-proposed-schema-changes-for-sign-off)
- [7. Testing](#7-testing)
- [8. Dependencies outside this plan](#8-dependencies-outside-this-plan)
- [9. Rollout](#9-rollout)
- [10. Risks](#10-risks)
- [11. Open questions](#11-open-questions)

---

## 1. Summary

About a third of the protocol already exists: batches, prefix-scoped STS (and
built-but-not-live R2) credentials, and DPS registration by server-side copy.
The rest is new:

| Area | Today | Work |
|---|---|---|
| Device identity and allowlist | None. Only vendor-wide `gik_` bearer keys | **New**: a `vendor_devices` registry (one row per rig, with its approval status and key), with an approval status, HMAC request auth, signed responses and receipts, sealed credentials |
| `initiate` | `POST /ingest/batches` + `/credentials` | **Adapt**: device-scoped continue-or-create, sealing, per-device limits |
| `commit` | `seal_batch`: one-shot, all-or-nothing, size-only | **Adapt**: verify one session (size + MD5 via one `LIST`), then seal it as its own **one-episode batch**; receipts, upload state, async hand-off |
| Registration | Per batch; needs per-part SHA-256 | **Reuse** `vendor-batch-ingest` on each one-episode batch; SHA-256 computed in the Container (D2) |
| Batch expiry | Hourly DPS sweeper | **Adapt**: must not strand or purge committed sessions |

Estimate: roughly **7–9 engineer-weeks** for backend and DPS, in six phases
(§5), plus firmware in parallel. The four blockers (§3) are resolved by the
decisions in §4; no new table is needed.

---

## 2. What exists today

**Reusable as-is or with small changes:**

- **Batch row as an upload.** `ingest_batches` (`BE/db/models/ingest_batch.py:76-146`),
  id `btch_` + 26 Crockford base32 (`BE/services/ingest_batches.py:174-183`),
  strictly validated because it goes into IAM ARNs. Usable as `upload_id`
  unchanged. `expires_at` = open + 168 h (`ingest_batches.py:597`).
- **Prefix-scoped credentials.**
  - S3: STS `AssumeRole` with an inline session policy on
    `v1/<slug>/<batch>/` (`BE/services/vendor_credentials.py:136-223`), 1 h
    default, role maximum 1 h (`infra/vendor-ingest/README.md:54`). Meets the
    protocol's ≥ 30 min.
  - R2: temporary credentials scoped to the prefix (`vendor_credentials.py:244-297`).
    Built, not live: production is still S3.
- **Folder layout.** The backend and DPS expect `<prefix>episodes/<episode_key>/<part>`
  (`ingest_batches.py:345-357`, `DPS/vendor_ingest/register.py:624`). Returning
  `prefix = v1/<slug>/<batch>/episodes/` and using `session_key` as
  `episode_key` gives the protocol's `<prefix><session_key>/<file>` with no
  path change. 32 hex passes `_EPISODE_KEY_RE` (`ingest_batches.py:82`).
- **Registration by server-side copy.** DPS `register_episode` copies plain
  MCAP parts with `copy_object` (`DPS/s3_objects.py:271-292`); bytes never
  enter the worker. Already-registered episodes are skipped by a DB lookup
  (`register.py:600-605`). `session.json` handling exists (`register.py:189-204`).
- **Hand-off and re-drive.** `POST /pipelines/batch/ingest` to DPS, with a
  reconciler re-drive for stuck batches (`ingest_batches.py:727-845`,
  `DPS/flows/reconciler.py:810-870`).
- **Crypto primitives.** `cryptography`'s HKDF and ChaCha20-Poly1305 are
  already used in backend tests (`BE/tests/test_vendor_device_key_clear.py`).
  Note that our existing QR sealing (visio-seal-v1) is a different, asymmetric
  scheme to a fleet public key; the protocol's credential sealing is
  symmetric over the device secret, so its envelope is new code.
- **Server-side SHA-256.** `CopyObject` with `ChecksumAlgorithm=SHA256` makes
  S3 store a full-object SHA-256, read back with
  `HeadObject(ChecksumMode="ENABLED")`. Proven on 2026-10-10 when datatang's
  stuck batches were sealed without re-upload.

**Not usable:**

- `seal_batch` (`ingest_batches.py:620-724`), **as a whole**: requires `open`,
  seals once, and fails the whole batch on any missing, wrong-size **or
  unlisted** object (`diff_against_listing`, `:360-393`). It lists the whole
  batch prefix and discards ETags (`BE/services/vendor_ingest_storage.py:40-53`),
  then calls DPS synchronously with a 10 s timeout (`config.py:811-814`). Its
  tail (build the canonical manifest, mark the batch `sealed`, hand off to DPS)
  is reused for one-episode session batches (D1); its listing and diff are
  not.
- Vendor auth (`BE/core/vendor_auth.py:138-238`): bearer `gik_` keys bound to a
  vendor, no device binding, no HMAC.

---

## 3. Blockers

1. **Partial commit vs batch-wide seal.** A commit for one session, while
   others are still uploading into the same prefix, fails `seal_batch` every
   time. The commit path needs its own per-session verification and record.
   *Resolved by D1:* each session is verified against its own folder and
   sealed as its own one-episode batch.
2. **Per-part SHA-256 is required end to end.** `parse_manifest` requires
   `parts[].sha256` (`ingest_batches.py:78, 284-288`), and both repos build the
   content digest as `sha256("<name>:<sha256>\n" …)`
   (`BE/services/episode_identity.py:67-83`, `DPS/episode_identity.py:72-88`).
   The protocol carries MD5, not SHA-256. Switching our digest to MD5 would
   fork the digest space: rig deliveries would stop deduping against CLI
   deliveries of the same footage (`register.py:110-186`), storing second
   copies.
3. **DPS writes batch status.** `run_vendor_batch_ingest` sets the batch to
   `ingesting`, then `completed` or `sealed` (`DPS/flows/vendor_batch_ingest.py:311-397`).
   A device upload stays open while its sessions are registered; those writes
   would close it. *Resolved by D1:* DPS only ever ingests the one-episode
   session batches, so its status writes land on those, never on the rig's
   open upload batch.
4. **The expiry sweeper.** `expire_stale_open_batches`
   (`DPS/flows/vendor_batch_expiry.py:94-188`, hourly) flips `open` batches
   past `expires_at` to `expired`, and purges their bytes when
   `VENDOR_BATCH_EXPIRY_PURGE` is on (default off). A long-lived device upload
   would be swept with committed-but-unregistered sessions in it.

---

## 4. Design decisions

Each needs agreement before the phase that depends on it.

**D1. Each committed session is sealed as its own one-episode batch.**
*(Decided 2026-10-21.)* There are two kinds of `ingest_batches` row:

- **The upload batch:** the rig's long-lived open batch (D3). Its id is the
  `upload_id`; it owns the folder (`v1/<slug>/<batch>/episodes/`) and the
  credentials. It is never sealed.
- **A session batch, one per committed session:** created by `commit`, after
  the session's files are verified. It is born `sealed`, with a canonical
  manifest holding exactly that one episode, `upload_id` pointing at the
  upload batch, the upload batch's `s3_prefix` (so the files are read where the
  rig put them), and `session_key`. It is handed to the existing
  `vendor-batch-ingest` flow like any sealed CLI batch.

This needs **no new table**: the session batch row is the durable commit
record, the hand-off queue (the reconciler already re-drives batches stuck in
`sealed`) and, through its status and `episodes.batch_id`, the registration
result. Re-commits are idempotent by looking up `(device_id, session_key)`: the
same manifest returns `committed` and the same receipt, a different one
`conflict`. Receipts are not stored; they are recomputed from the device key,
`upload_id`, `session_key` and `content_digest`. The session batch's manifest
is kept in its `manifest` column only; no `manifest.json` is written to the
shared upload prefix, where every session's would collide.

**D2. SHA-256 is computed during registration, in the Cloudflare Container,
so the digest space stays one.** *(Decided 2026-10-21.)* Registration
stream-reads each committed part once and, in that single pass, computes its
SHA-256, re-checks its MD5 against the commit, and decrypts it if it is VREC.
The SHA-256 fills the canonical manifest's `sha256`, so identity, dedup and
`episode_raw_sha256` stay exactly as today, and rig deliveries still dedup
against CLI deliveries. The protocol's MD5-based `content_digest` is used only
in receipts to the rig, never as our dedup key.

**This makes device ingest R2-only.** A Container's reads from R2 are free;
its reads from S3 leave AWS at ~$0.09/GB. Device uploads therefore go to
`gilabs-r2-ingest`, and the plan depends on the R2 plan's open items: the
bucket and its secrets (`r2-phase0-findings.md`), and registration running in
Cloudflare Containers (R2 plan, Phase 2). The in-place `CopyObject` SHA-256
trick stays available for S3 batches, but is not this plan's path.

**D3. Device uploads are long-lived batches, ingested session by session.**
*(Decided 2026-10-21.)* `ingest_batches` gains `device_id`, and each rig has
one open batch. GILabs **keeps it open**: it is never sealed as a whole, and
every commit response says `upload.state: "open"`. Each committed session is
registered on its own, as its own one-episode batch (D1), as soon as it is
committed, so ingestion is continuous while the upload batch stays open. DPS
never touches the upload batch's status.

- **Expiry is rolling.** Each `initiate` and each commit pushes `expires_at`
  to now + 7 days, so only a rig idle for a week has its batch expired; its
  next `initiate` opens a new one.
- `closed` stays in the protocol for customers who want it; GILabs does not
  use it. `410 upload_closed` is only for an expired or abandoned batch.

**D4. The allowlist is the `vendor_devices` registry.** Each rig a vendor
uses has one row: its serial, its approval `status` (`pending` / `approved` /
`blocked` / `retired`) and its current device key. Anything but `approved`
answers `403 unauthorized`. Revoking the key (`key_revoked_at`) answers
`403 revoked`, and the rig needs a new key. A key is minted only for a rig that
an admin or vendor owner has added to the registry. *(Decided 2026-10-21:)* **vendor owners and GILabs
admins** can both approve, block and revoke a vendor's rigs; every change
records who made it.

**D5. The episode date is derived server-side by the CLI's rules.** The
protocol's commit has no `date`. DPS derives it from `session.json`
`start_time_unix`, falling back to the session folder suffix, using a port of
the CLI's `ego_session_date` (`vendor-cli/gi_ingest/scanner.py:525-568`), not
the generic `extract_date`, which mis-reads `session_NNNNN-<unix>`. It lives
in each repo's `episode_identity` module with a golden vector, like the id
itself, so a rig delivery and a CLI delivery of the same session get the same
id and dedup. `session_key` is used only as the path (`episode_key`); the uuid5
input stays `session` (the card folder name).

**D6. Each rig's key is derived by the backend from a per-vendor master
secret; nothing secret is stored per rig.** *(Decided 2026-10-21.)*

```
device_secret = HMAC-SHA256(vendor_master, device_id + ":" + key_version)
```

- **One master secret per vendor**, 32 random bytes in AWS Secrets Manager,
  the pattern the vendor recording keys use (`vendor_recording_keys.py:39-72`).
  Created when the vendor's first rig is issued a key. The backend loads it
  once per process and caches it, so verifying a request costs one HMAC to
  re-derive the rig's secret and one to check the signature, with no network
  call.
- **Still one key per rig.** A secret pulled off one rig works only as that
  `device_id`, so the allowlist (D4) keeps its meaning.
- **Rotating or revoking one rig** bumps its `key_version`: the old derived key
  no longer verifies, and the rig needs a new QR.
- **The trade-off:** a leak of a vendor's master secret from our side exposes
  every rig of that vendor at once. Access to it is limited to the backend's
  role (§10). Rotating a master means re-issuing every rig of that vendor.

**Who generates and delivers it.** The rig never generates its own key: it has
no trusted way to hand a new secret to the server, and issuing it server-side
makes "approve a rig" and "give it a key" one step for a person.

1. A vendor owner or admin issues a key for an approved rig in the portal.
2. The backend sets the rig's `key_version`, generates a random `key_id`
   (prefixed, e.g. `dk_…`), and derives the secret as above.
3. The portal shows a settings QR carrying the endpoint, `key_id` and the
   secret, the secret sealed to the fleet public key (visio-seal-v1) so the QR
   image alone does not reveal it.
4. The operator scans it with the companion app, which passes it to the rig
   (`SetStorage`); the rig unseals and keeps it in `/userdata`.
5. The secret is never stored or shown again: the backend re-derives it when
   it needs it. A lost or leaked key is replaced by rotation (new `key_id`,
   next `key_version`), which voids the old one at once.

**D7. A separate router and feature flag.** *(Decided 2026-10-21: the rig's
URL is a path on the main API, not a dedicated hostname.)* `/api/v1/device-upload` mounts
`POST /v1/uploads/initiate` and `POST /v1/uploads/{upload_id}/commit`, behind
`DEVICE_UPLOAD_ENABLED` (default off) as well as `VENDOR_INGEST_ENABLED`.
The rig's endpoint is `visio+https://<api-host>/api/v1/device-upload`. The
R2 client-version gate (`ingest_batches.py:519-554`, `X-Client-Version`) does
not apply to device requests.

**D8. Credential scope is one rig's prefix.** *(Decided 2026-10-21.)* R2
temporary credentials are `object-read-write`, wider than the protocol's
"create only", and that is accepted, because they are scoped to the rig's own
upload batch's prefix (`v1/<slug>/<batch>/episodes/`) and each upload batch
belongs to exactly one rig (D3). A rig's credentials can never reach another rig's or another
vendor's data.

---

## 5. Phases

### Phase 0: spikes (≈ 1 week)

Answer the questions the design rests on, against real buckets:

- [ ] `gilabs-ingest` default encryption: SSE-S3 (ETag = MD5) or SSE-KMS (it
      is not). `infra/vendor-ingest/` does not say.
- [ ] Single-PUT ETag = MD5 on R2 and on `gilabs-r2-scratch`.
- [ ] Container stream-hash throughput (D2): SHA-256 + MD5 over a 256 MiB
      part read from R2, and per-session registration time.
- [ ] `LIST Prefix=<prefix>episodes/<session_key>/` latency for a 160-part
      session, against the 5 s commit budget.
- [ ] STS and R2 mint latency, and R2's mint rate limit, for a fleet of rigs
      renewing hourly.

### Phase 1: device identity and allowlist (≈ 2 weeks)

- [ ] Migration: `vendor_devices` (§6.1), after sign-off.
- [ ] Key service (D6): per-vendor master secret in Secrets Manager
      (create on first issue, cached per process); derive each rig's secret
      from it with `key_version`; generate `key_id`; render the sealed
      settings QR; approve, block, retire, revoke, rotate (bump
      `key_version`). Audit who did what. Nothing secret is stored per rig.
- [ ] Admin and vendor-portal endpoints: add a rig by serial, approve, block,
      revoke; list rigs with `last_used_at`.
- [ ] Settings QR: deliver endpoint, `key_id` and secret sealed. No new
      sealed field: the QR's existing slots carry it, the same ones the
      settings-QR ingest section uses (`endpoint_url` = `visio+https://…`,
      `access_key_id` = `key_id`, the secret sealed as `sk`, `dev` = the rig's
      serial so the QR works only on that rig). The backend builds it with the
      pinned `0.9.1` `seal_into`, which needs no bump.
- [ ] Portal UI for the above (vendor-frontend).

### Phase 2: request auth and `initiate` (≈ 1.5 weeks)

- [ ] FastAPI dependency `require_device`: rebuild the string-to-sign,
      constant-time compare, ±15 min skew (`401 clock_skew`), `device_id`
      binding (`403 device_mismatch`), status check (`403 unauthorized`,
      `403 revoked`), `last_used_at` (throttled). Golden vectors from the
      `visio-schema` reference implementation.
- [ ] Response signing (`X-Visio-Signature`) and credential sealing (HKDF +
      ChaCha20-Poly1305), both against the shared vectors.
- [ ] Migration: `ingest_batches.device_id`, the one-open-batch-per-device
      index (§6.2); rolling `expires_at` (D3).
- [ ] `initiate`: continue-or-create (look up the device's open batch;
      `null` `previous_upload_id` returns it, D3), mint credentials for
      `…/episodes/`, drop `ListBucket` from the device session policy (the rig
      never lists), add `device_id` to the STS `RoleSessionName`.
- [ ] Record the rig's reported `recording_key_fingerprint` on its
      `vendor_devices` row (§6.1) and compare it with the vendor's current
      recording key: flag a stale key (the vendor rotated) or an unencrypted
      rig where the vendor requires encryption, in the portal. The upload is
      still accepted (accept every session); the flag is for the operator.
- [ ] `test: true`: authenticate, decide routing, mint nothing.
- [ ] Per-device open-batch ceiling and rate limits. Today the ceiling counts
      vendor-wide when there is no user (`ingest_batches.py:401-418`), which
      would refuse a fleet of more than 16 rigs.

### Phase 3: `commit` (≈ 2 weeks)

- [ ] Migration: `ingest_batches.upload_id` and `session_key` (§6.2).
- [ ] Per session, within 5 s: validate the entry, one `LIST` of the
      session's folder, compare size and ETag/MD5 (and nothing else). States
      `committed`, `pending` (only if verification cannot finish, e.g.
      SSE-KMS), `incomplete`, `invalid`, `conflict`.
- [ ] On success, create the **session batch** (D1): born `sealed`, one-episode
      canonical manifest (`episode_key = session_key`, `session` = folder name,
      parts with `bytes` and `md5`, `metadata_file` = `session.json`),
      `upload_id`, the upload batch's `s3_prefix`, `device_id`,
      `session_key`. Factor `seal_batch`'s tail (manifest build, mark sealed,
      hand off) so both paths share it.
- [ ] Idempotency: an existing session batch for `(device_id, session_key)`
      with the same manifest answers `committed` with the recomputed receipt;
      a different manifest answers `conflict`.
- [ ] `upload` block on every response; GILabs always answers `open` (D3).
      Each commit extends the upload batch's `expires_at` by 7 days.
- [ ] Async hand-off: the commit request never waits on DPS. Hand-off is
      fire-and-forget after the response; a failed hand-off leaves the session
      batch `sealed`, which the existing reconciler re-drives
      (`DPS/flows/reconciler.py:810-870`).
- [ ] `410 upload_closed` only for expired or abandoned upload batches.

### Phase 4: registration of session batches (≈ 1.5 weeks)

- [ ] `vendor-batch-ingest` runs unchanged per session batch (one episode
      each), in the Cloudflare Container (D2). Its status writes land on the
      session batch only.
- [ ] Manifest without SHA-256: accept device-sourced manifests whose parts
      carry `md5` instead of `sha256` (backend `parse_manifest`, DPS
      `register_episode`). Registration streams each part once from R2,
      computing SHA-256, re-checking MD5 and decrypting VREC in the same pass,
      and fills `sha256` before identity and dedup run (D2).
- [ ] Port `ego_session_date` into `episode_identity` in both repos, with a
      golden vector, and derive `date` for session batches (D5).
- [ ] Sweeper (blocker 4): upload batches expire only after 7 idle days
      (rolling `expires_at`, D3); expiring stops new commits; never purge an
      upload batch's prefix while any of its session batches is not yet
      `completed`.
- [ ] Portal: show a rig's session batches grouped under its upload batch
      (`upload_id`), so one rig does not appear as hundreds of one-episode
      batches.

### Phase 5: end-to-end and pilot (≈ 1.5 weeks)

- [ ] A reference rig client (Python, from the `visio-schema` reference
      implementation) that runs initiate → PUT → commit against staging, used
      in CI and for the firmware team's integration tests.
- [ ] Staging with one bench rig on firmware that implements the API.
- [ ] Pilot with one vendor and a few rigs, CLI path unchanged in parallel.

---

## 6. Proposed schema changes (for sign-off)

Sketches for review. No migration is written until these are agreed. No new
table is needed for commits (D1); the session batch is an `ingest_batches`
row.

Types follow the existing vendor-ingest tables
(`supabase/migrations/20260804000000_add_vendor_ingest.sql`): vendor, user and
batch ids are `TEXT`, with the same `ON DELETE` behaviour as
`vendor_api_keys` and `ingest_batches`.

### 6.1 `vendor_devices`

The registry of rigs a vendor may upload from: the allowlist (D4), with each
rig's current device key on its row.

```sql
create table public.vendor_devices (
  id              uuid primary key default gen_random_uuid(),
  vendor_id       text not null references public.vendors(id) on delete cascade,
  device_id       text not null check (device_id ~ '^[0-9a-f]{16}$'),
  label           text,             -- e.g. hostname GILABS-…, or the vendor's own name for it
  status          text not null default 'pending'
                  check (status in ('pending','approved','blocked','retired')),
  approved_by     text references public.users(id) on delete set null,
  approved_at     timestamptz,
  -- current device key; the secret itself is derived, never stored (D6):
  --   HMAC-SHA256(vendor_master, device_id || ':' || key_version)
  key_id          text unique,      -- null until a key is issued; new on each rotation
  key_version     integer not null default 0,   -- 0 = never issued; bumped on each issue
  key_issued_at   timestamptz,
  key_revoked_at  timestamptz,
  last_used_at    timestamptz,
  -- recording encryption, as the rig last reported it (initiate, session.json)
  recording_key_fingerprint  text,          -- SHA-256(key)[:8] hex; '' = records unencrypted
  recording_key_reported_at  timestamptz,
  created_by      text references public.users(id) on delete set null,
  created_at      timestamptz not null default now(),
  unique (vendor_id, device_id),
  check ((key_id is null) = (key_version = 0))
);
```

The recording-encryption columns record what the rig **reports**; the
vendor's expected key stays per vendor, where it is today
(`vendor_recording_keys.py`). Comparing the two flags a rig on a stale key or
recording unencrypted (Phase 2). Registration does not depend on them: it
picks the decryption key from each session's own fingerprint in
`session.json`, so a stale-key rig's recordings still decrypt as long as the
old key is kept.

One row per rig per vendor. Rotating a key sets a new `key_id` and the next
`key_version`; the old key stops verifying at once. The vendor master secrets
live in Secrets Manager, not in this table. Who approved, blocked or
revoked what, and when, goes to an audit log rather than extra columns.

### 6.2 `ingest_batches`

```sql
alter table public.ingest_batches
  add column device_id       text check (device_id ~ '^[0-9a-f]{16}$'),
  add column upload_id text references public.ingest_batches(id) on delete cascade,
  add column session_key     text check (session_key ~ '^[0-9a-f]{32}$');
-- upload batch:  device_id set, upload_id null, session_key null
-- session batch: device_id set, upload_id = the upload batch, session_key set,
--                born 'sealed' with a one-episode manifest (D1)
-- no new status: GILabs keeps upload batches open (D3); expires_at, already
-- present, is rolled forward on each initiate and commit

-- one open upload batch per rig
create unique index ingest_batches_one_open_upload_per_device
  on public.ingest_batches (vendor_id, device_id)
  where device_id is not null and upload_id is null and status = 'open';

-- one session batch per committed session: idempotent re-commit, conflict detection
create unique index ingest_batches_one_session_batch
  on public.ingest_batches (device_id, session_key)
  where session_key is not null;
```

Per the migration-CI lesson: verify the new `ingest_batches` index against
production rows (no vendor may already have two open batches with the same
`device_id`) before merging, not only against a fresh database.

---

## 7. Testing

- **Golden vectors:** request signing, response signing, receipts and
  credential sealing, from one reference implementation in `visio-schema`,
  asserted in backend tests and in firmware tests.
- **Unit:** auth dependency (every error code), continue-or-create races (two
  concurrent `initiate`s for one device), commit idempotency and `conflict`
  (two concurrent commits of one session create one session batch), `pending`
  path.
- **Integration (moto or a scratch bucket):** LIST + ETag verification, the
  session batch's files read from the upload batch's prefix, Container
  stream-hashing, sweeper interplay.
- **End-to-end:** the reference rig client against staging (Phase 5), including
  interruptions: drop mid-PUT, lost commit reply, lost initiate reply, reboot
  (credentials lost, `previous_upload_id` kept).
- **Identity:** a session delivered by the CLI and again by a rig gets the same
  `episode_id` and is recorded as a duplicate, not a second copy.

---

## 8. Dependencies outside this plan

- **visio-schema:** the reference implementation and golden vectors for
  signing, receipts and sealing (`visio_schema.ingest`,
  `tests/golden/ingest_vectors.txt`) and the settings-QR ingest section, both
  on `dev` since `06e8bbc`. The backend's port reproduces those vectors byte
  for byte. The reference rig client, `visio_schema.ingest.client`. A release
  the backend can pin.
- **Firmware (device team):** MD5 while streaming (CPU cost still to be
  measured), the random `session_key`, hold mode while recording, and
  `403 unauthorized` handling. Phase 5 needs a bench build.
- **Companion app:** the `visio+https` provider row and settings-QR fields.

---

## 9. Rollout

1. Phases 1–4 behind `DEVICE_UPLOAD_ENABLED=false`.
2. Staging: one bench rig, on R2 (D2).
3. Pilot: one vendor, a few approved rigs, CLI unchanged in parallel; compare
   episode counts and dedup against the CLI ledger.
4. General availability per vendor, by approving its rigs.
5. Device ingest is R2 from the start (D2). It cannot launch before
   `gilabs-r2-ingest` exists and registration runs in Containers (R2 plan,
   Phase 2); the CLI path can move to R2 on its own schedule.

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| ETag is not MD5 (SSE-KMS, multipart) | Phase 0 check; `pending` + read-back fallback; keep the bucket on SSE-S3 |
| 5 s commit budget missed on huge sessions | One `LIST` per session (≤ 1000 keys per page), async hand-off; measured in Phase 0 |
| R2 mint rate limit across a fleet | Mint only on renewal (< 15 min left), not per `initiate`; measured in Phase 0 |
| Leaked device key | One device per key, revocation, allowlist status, `last_used_at` monitoring |
| Leaked vendor master secret (D6) exposes all that vendor's rigs | Secrets Manager, readable only by the backend's role, never logged; a documented master-rotation procedure that re-issues the vendor's rigs |
| Digest-space fork | D2 keeps SHA-256 as the dedup key |
| R2 plan slips (bucket, Containers) | Device ingest waits for it (D2); nothing in Phases 1–3 depends on it, so they can proceed |
| Dead-RTC dates collide | Same `ego_session_date` rules as the CLI; the digest discriminator (`register.py:127-186`) still separates true collisions |
| Sweeper purges committed data | Phase 4 sweeper change; purge stays off until then |
| Hundreds of one-episode batches per rig clutter the portal and batch metrics | Group session batches under their upload batch (`upload_id`) in the portal and in reports |

---

## 11. Open questions

**All decided, 2026-10-21:**

- **Who approves a rig:** vendor owners and GILabs admins (D4).
- **Commit records:** each committed session is a one-episode batch; no new
  table (D1).
- **SHA-256:** stream-hashed in the Cloudflare Container during registration;
  device ingest is R2-only (D2).
- **R2 credential scope:** accepted, as one rig's upload-batch prefix (D8).
- **Batch policy:** GILabs keeps upload batches open and ingests each committed
  session continuously, with rolling expiry (D3).
- **Rig URL:** `visio+https://<api-host>/api/v1/device-upload`, a path on the
  main API (D7).
