# Rig Ingest API: implementation status (overnight build, 2026-10-22)

Everything in the plan ([`ingest-api-implementation-plan.md`](ingest-api-implementation-plan.md))
that can be built and tested without production access is built, and open as
draft PRs. **Nothing is merged, deployed or applied anywhere.** Both feature flags
default off.

Where the plan was wrong or silent, I took the option below and kept going. Each
one is listed in §3 so you can overturn it. The questions that are yours are in §4.

## 1. What is where

| Repo | Branch | Worktree | Commits |
|---|---|---|---|
| visio-schema | `feat/ingest-reference-client` (from `origin/dev` `0302d4d`) | `episode-outcomes-visio-schema/.claude/worktrees/rig-ingest-api-spec` | the reference rig client, the plan and this report (see §1.1) |
| data-platform | `feat/rig-device-upload` (from `origin/main` `6f9b6c9a`) | `data-platform/.claude/worktrees/rig-device-upload` | `01a88af8` schema · `c31e3a66` API and registry · `3cca0940` OpenAPI · `6e96f74c` CLI isolation, portal grouping · `9089cd7c` S3 endpoint fix · `39263c04` test scoping · `e01916fc` `b981c05f` `0db636ea` portal UI · `44b35c0a` retired-rig fix · `32c312dd` held to `dev`'s vectors too · draft PR gilabs-data-center#788 |
| DPS | `feat/device-upload-session-batches` (from `origin/main` `fa097c8`) | `data-processing-services/.claude/worktrees/device-upload-session-batches` | `5e39e8e` model mirror · `8bf1cd2` SHA-256 pre-pass · `f1e83cb` scoped purge · `f0d49c4` reconciler re-drive · `a1a7493` sweeper guard · draft PR data-processing-services#417 |

Firmware and the companion app are not touched, as asked.

### 1.1 visio-schema

- **On `dev` already (a teammate's `06e8bbc`):**
  - the protocol's reference cryptography, `visio_schema.ingest`, and its golden vectors;
  - the settings-QR ingest section;
  - the proto fields and companion-app commands.

  I had written my own reference module and QR row before seeing it. They are not in this PR. My implementation, and the backend's port, reproduce every one of `dev`'s vectors byte for byte.
- **New in this PR:** `visio_schema.ingest.client`, a reference rig client built on that module. Stdlib only, SigV4 or Azure SAS PUTs, and its SigV4 is checked against botocore's.
- **Not in this PR:** the original design branch (`worktree-rig-ingest-api-spec`), with its internal spec, stays local for history.
- Tests: 734 pass. One unrelated pre-existing test (`test_concurrent_writers_do_not_lose_each_others_keys`) fails on macOS's multiprocessing spawn, with or without these changes.

### 1.2 data-platform backend

- **Migration** `20261022090000_add_vendor_devices_and_device_uploads.sql`:
  - `vendor_devices`, with RLS on and anon/authenticated revoked;
  - `ingest_batches.device_id / upload_id / session_key`, with CHECKs and the two partial unique indexes.
- **`/api/v1/device-upload/v1/uploads/initiate`** and **`/{upload_id}/commit`**, behind `DEVICE_UPLOAD_ENABLED` and `VENDOR_INGEST_ENABLED`.
- **Device keys** are derived per rig from a per-vendor master in Secrets Manager. Nothing secret is stored.
- **Registry**:
  - `/vendors/{id}/devices`: portal, for vendor owners and uploaders;
  - `/admin/vendors/{id}/devices`: org admins.

  Both cover add, approve, block and retire, issuing a key (which returns the device QR), and revoking it.
- **Batch listings**:
  - The CLI's batch lookups and listing never see a rig's batches.
  - The portal lists a rig as its upload batch, with session counts, and `?upload_id=` lists its sessions.
- **Tests**:
  - 2668 pass, 190 skipped, coverage 81.7%.
  - black and ruff are clean, and the OpenAPI spec and TS types are regenerated.
  - `test_device_upload_flow.py` (24 tests) runs the protocol against real Postgres when `DEVICE_UPLOAD_TEST_DATABASE_URL` is set; CI skips it.

### 1.3 DPS

- Session batches register through the unchanged `vendor-batch-ingest` flow.
- For parts that carry only `md5`, a pre-pass streams each object once and computes SHA-256 + MD5 over the delivered bytes. A mismatch refuses the episode. CLI batches take exactly the old path, with zero extra reads.
- `purge after copy` deletes only the session's own folder for a session batch.
- A new reconciler pass gives a session batch whose fire-and-forget hand-off never arrived the job row the existing re-drive needs. It runs after a 10-minute grace, at most 10 per tick.
- The sweeper never purges an upload batch's prefix while any of its sessions is not `completed`.
- Tests: 592 pass (I re-ran them independently).

### 1.4 Vendor portal UI

- **A Rigs tab** (中文 "设备").
  - It lists each rig with: status, key state, last seen, and recording-key status with an explanation when stale or unencrypted.
  - **Add rig** takes the serial. A pasted `GILABS-…` name is refused with a specific message.
  - **Owners** can approve, block and retire a rig, and issue or revoke its key. Issuing shows the QR exactly once, warns that re-issuing voids the old key, and offers a project picker when the vendor delivers to several projects. Confirmations are in-app, never `window.confirm`.
  - **Uploaders** can add a rig; **viewers** read only.
- **Deliveries** shows a rig as one row ("Rig <serial>, N sessions / M registered"), which expands to list its sessions, paged.
- The logic lives in `src/lib/rigs.ts` (13 node tests).
- Gates: lint, type-check, 104 tests and `yarn build` all pass. I re-ran the first three myself.
- The panels were checked in Chrome on a throwaway route, since removed. Not checked visually: the Chinese strings, phone widths.

## 2. How it was verified

1. **Unit and golden-vector tests in all three repos.** The backend's crypto port reproduces visio-schema's vectors byte for byte.
2. **The migration against real Postgres.**
   - Every one of the 153 migrations applies to the Supabase Postgres 17 image.
   - The new migration runs cleanly over a pre-existing CLI batch.
   - Every designed refusal fires: a second open upload, a duplicate session, a malformed session batch, a hostname as serial, a key without a version, and a duplicate rig.
   - RLS is on, and anon/authenticated have no grants.
3. **End to end against the live backend.** This ran uvicorn on the test database, with moto standing in for STS, Secrets Manager and S3, and DPS unreachable on purpose. Starting from a clock 2 h slow, the reference client:
   1. recovered from `clock_skew` using `Date`;
   2. ran a `test` initiate (nothing created);
   3. initiated, then continued the same upload;
   4. committed a session, with its receipt verified;
   5. got `incomplete` with the missing part named, then `committed` once it landed;
   6. got the identical receipt on a re-commit.

   In the database:
   - the upload stayed `open`;
   - each session became a `sealed` batch, dated `20260808` from `session.json`;
   - the hand-off ran after the reply and recorded "will retry";
   - the rig's `last_used_at` was set.

   This run found two bugs, both fixed:
   - the reference client sent its PUT as a form body;
   - S3 rigs were pointed at AWS even with a staging endpoint configured (`9089cd7c`).

   The harness is saved in `~/repo/gilabs/tmp/device-upload-e2e/` (local, not committed).

To re-run the Postgres tests yourself:

```sh
sh ~/repo/gilabs/tmp/device-upload-e2e/reset_testdb.sh        # throwaway container on 127.0.0.1:55439
sh ~/repo/gilabs/tmp/device-upload-e2e/apply_migrations.sh <data-platform worktree>/supabase/migrations
cd <data-platform worktree>/backend
DEVICE_UPLOAD_TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55439/postgres \
  ../../../backend/.venv/bin/python -m pytest app/tests/test_device_upload_flow.py -q --no-cov
docker rm -f gilabs-device-upload-testdb                    # when done
```

I deliberately did not use `supabase start`. It reuses the main checkout's `project_id`, so it would share your local Supabase's Docker volumes.

## 3. Choices made without asking

| # | Where | The question | What I did | The alternative |
|---|---|---|---|---|
| 1 | QR | The plan wanted a new `SealedSecrets` device-key field | **No new field.** `access_key_id` = key id, the secret sealed as `sk`, and `dev` pins the QR to the rig: the same slots `dev`'s settings-QR ingest section uses. | A new field: needs a firmware change and a new companion-app parser for no gain |
| 2 | Backend crypto | The backend pins visio-schema 0.9.1 from PyPI, which has no `ingest` module | **Ported it** (`device_upload_crypto.py`), held to both sets of golden vectors (`dev`'s included), the way `episode_identity` is. | Release visio-schema first and bump the pin. It can become a re-export later. |
| 3 | Device QR | Does issuing need a visio-schema bump? | **No.** 0.9.1's `seal_into` seals without validating the endpoint. | — |
| 4 | Destination | Which project does a rig deliver to? | `destination_hint` = a project id. With one grant it can be empty; with several the QR must name one, and the portal requires it when issuing. Same rule as gi-ingest. | A project column on `vendor_devices`: a binding you rejected for keys |
| 5 | Destination errors | Status for "no grant / must choose" | `409 no_compatible_storage`, so the rig retries next run once an admin fixes the grant | `403`, which would stop the rig until it is re-provisioned |
| 6 | Session date (D5) | Who derives it | **The backend, at commit.** It already reads `session.json` within the budget, so DPS receives a complete manifest with `date` and `episode_id`. DPS has no date code. | DPS derives it, as D5 said |
| 7 | Session date | The CLI's mtime step and factory-clock prompt have no server equivalent | mtime dropped. A factory clock (missing, < 1e9, or > 365 days old) is dated by the **commit time**, and the manifest records `date_basis: "commit_time"`. | The `00000000` sentinel, or the 2021 factory date |
| 8 | Provider (D2) | R2-only blocks staging until the R2 bucket exists | `DEVICE_UPLOAD_PROVIDER=r2` by default; `s3` allowed, with a **PutObject-only** session policy and `RoleSessionName=dev-<serial>-<batch>` | R2 only |
| 9 | Credential lifetime | R2 credentials took the batch's whole window (~7 days with rolling expiry) | `DEVICE_UPLOAD_CREDENTIAL_SECONDS` = 1 h for both providers (R2: at most the batch window) | Longer, which means fewer mints |
| 10 | "Admin" (D4) | Which admin | **Org admin/owner of the vendor's owning org**, the existing vendor-admin model, plus vendor **owners** in the portal. Uploaders may add a rig, which stays `pending`. | Platform superuser |
| 11 | Audit (D4) | "Every change records who made it" | `approved_by_user_id` / `approved_at` on the row, plus one log line per change, as `vendor_admin` does today. **No audit table.** | A new audit table |
| 12 | DDL vs §6.1 | Id type and names | `TEXT` id (app uuid4), `approved_by_user_id`, named CHECKs, RLS + REVOKE, plus the plan's columns. This follows the existing vendor tables. | `uuid` id with `gen_random_uuid()` |
| 13 | Upload continuity | `previous_upload_id` | Ignored: a rig has exactly one open upload. If it is stale, or its project/provider/bucket no longer matches the rig's configuration, it is marked `expired` (not purged) and a new one opens. | Honour the rig's id |
| 14 | ETag not an MD5 | Multipart or SSE-KMS | The file is reported `mismatched`, so the rig re-sends it as a single PUT. There is no read-back path. **An SSE-KMS bucket would loop**, which Phase 0 must rule out. | `pending` plus a background read-back |
| 15 | Commit budget | 5 s | 4 s for verification; anything unfinished, or a storage error, is `pending` | — |
| 16 | Receipt binding | A session committed under an older upload, re-committed under a new one | The receipt binds the **URL's** `upload_id`, which is what the rig checks | The original upload's id: the rig could never verify it |
| 17 | Race with the sweeper | — | Commit takes the upload row `FOR UPDATE`; the sweep uses `SKIP LOCKED` | — |
| 18 | CLI vs rig batches | A `gik_` key could refresh, seal or **abandon** a rig's upload | The CLI's lookups and listing never see rig batches | — |
| 19 | Open-batch ceiling | A rig's upload counted toward the vendor-wide CLI ceiling | Rig uploads are not counted | — |
| 20 | Rate limit | The shared limiter is per IP | **None on the device routes**: a whole fleet usually shares one site NAT. Each call costs a signature check first. | Per-IP limit |
| 21 | Recording key (Phase 2) | Where the stale-key comparison happens | `initiate` only records the fingerprint; the device list compares it with the vendor's key and shows `current`, `stale`, `unencrypted`, `unreported` or `unknown`. Keeps Secrets Manager off the rig's path. | Compare in `initiate` |
| 22 | Issuing | Rotation and retiring | Issuing again = rotation (`key_version + 1` voids the old key). `retired` is final. The QR has no expiry (rigs have no reliable clock); revocation is the control. | — |
| 23 | Endpoint in the QR | No public-URL setting existed | `DEVICE_UPLOAD_PUBLIC_BASE_URL`, falling back to the request's base. **Set it in production.** | — |
| 24 | `Date` header | The rig sets its clock from it | Relies on uvicorn's (verified live). **Never run with `--no-date-header`.** | Set it ourselves: would send two |
| 25 | DPS, D2 "single pass" | Hash, check MD5 and decrypt in one read | **Two reads for VREC parts** (hash, then the existing land path). Plain parts are still one server-side copy. The single pass would stage a whole session on a 20 GB Container disk. | Rework `land_encrypted_object` |
| 26 | DPS re-drive | The plan assumed the reconciler re-drives any sealed batch | It only re-drives batches that have a job row, so a new pass creates one for orphaned session batches (10 min grace, 10 per tick) | Backend-side retry |
| 27 | DPS sweeper | When may an upload's bytes go | Only once every session batch under it is `completed` | — |
| 28 | Retired rigs | The registry is unique per (vendor, rig), so a retired rig could never come back; and a new row would restart at key version 1, re-deriving its old key | Adding a retired rig again **revives its row**: the version counter is kept and any old key stays revoked, so it needs a newly issued key | Drop the unique constraint |
| 29 | Portal UI | Its own calls | A separate Rigs tab. Only retire and revoke ask for confirmation. The QR is shown once, with no PNG download. The rig list isn't fetched from Deliveries, because it costs a Secrets Manager read. | — |

## 4. Questions for you

1. **Deploy order matters.**
   - The migration must apply **before** the DPS deploy: DPS's model now maps the three new columns, and every batch load (CLI batches included) breaks on a database without them.
   - The backend can ship with the flag off at any point after the migration.
   - Should I write this into each branch's description?
2. **The migration was written ahead of your DDL sign-off**, because you asked me not to block. Please review `20261022090000_add_vendor_devices_and_device_uploads.sql` against §6 of the plan, and choice #12 above.
3. **Phase 0 still needs real buckets.** R2 single-PUT ETag = MD5, the ingest bucket not on SSE-KMS (#14), LIST latency, mint rate limits. Run them yourself, or should I write the commands?
4. **Factory-clock dating (#7).** Is "date by commit time, mark `date_basis`" right? A CLI delivery of the same dead-clock session would get the operator's date instead, so the two would not dedup.
5. **Bytes left behind.** When an expired upload's prefix is retained because sessions were unfinished, nothing purges it later. Purge is off by default today. Do we want a follow-up sweep?
6. **Extra error codes.** The API also answers `not_enabled` (404), `bad_request` (400), `too_large` (413) and `unavailable` (503). The protocol's "other 4xx/5xx" rows cover them. Add them to the docs' error table?
7. **Admin console UI.** The org-admin registry endpoints exist, but there is no page for them in the data-center frontend; only the vendor portal UI was built. Do you want one?
8. **`hardware_units`.** It catalogues RV1106 units by SoC serial per org. If `chip_id` equals the OTP serial, `vendor_devices` could link to it. Worth doing?
9. **Delivery-list order.** The list is sorted by `opened_at`, and a rig's upload batch stays open for weeks, so a rig uploading right now sinks below newer CLI deliveries. Sort by last activity instead? That would also reorder CLI deliveries.
10. **Status filter.** On the default list, filtering by "rejected" can't find a rig's failed *session*, only upload batches; those sessions show when the rig is expanded. Should the filter also search sessions?
11. **Next steps outside this build:**
   - the companion app must accept the `visio+https` row;
   - the firmware items in the plan's §8;
   - marking the draft PRs ready, in order (#788 first, then #417).
