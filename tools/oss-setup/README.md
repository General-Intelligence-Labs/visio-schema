# `oss-setup` — provision an Aliyun OSS bucket for a fleet

Creates everything a bucket needs to receive recordings and status reports from
Visio devices, then **proves the keys work** by replaying the exact requests the
device, the app and the [fleet-status dashboard](../fleet-status/) make.

Stdlib only — no `pip install`, no `aliyun` CLI, no `visio_schema`. Copy the one
file and run it.

```bash
# interactive: asks for region and bucket, then does everything
python oss_setup.py

# non-interactive
python oss_setup.py --region cn-hangzhou --bucket my-recording-bucket

# show what would happen; signs nothing, needs no credentials
python oss_setup.py --region cn-hangzhou --bucket my-bucket --dry-run

# also save the device settings to a file instead of reading them off the screen
python oss_setup.py --region cn-hangzhou --bucket my-bucket --json out.json
```

Admin credentials (needed to create buckets and RAM users) come from
`ALIYUN_ACCESS_KEY_ID` / `ALIYUN_ACCESS_KEY_SECRET`, from `--ossutil-config` with
`--profile`, or from an interactive prompt. They are never written to disk.

## What it creates

Each step is idempotent, so it is safe to re-run after a failure.

1. the bucket, private ACL
2. RAM user + **key A** — used by both the device and the phone app
3. RAM user + **key B** — used by the fleet-status dashboard, read-only
4. a CORS rule, so the dashboard can read the bucket from a browser
5. a lifecycle rule expiring the status prefix (`--status-retention-days`, default 30)
6. verification with the freshly minted keys

## The permission contract

This table is the whole point of the tool — two keys, minimum grants, and a
reason for each one.

| Key | Grant | Scope | Why |
|---|---|---|---|
| **A** | `oss:PutObject` | `<bucket>/*` | the device uploads recordings + status reports; the app uploads in phone-record mode |
| **A** | `oss:ListObjects` | `<bucket>`, conditioned on the recordings prefix | the app's cloud recordings list, and the app's "Test" button (the device probes with a `max-keys=1` list) |
| **B** | `oss:GetObject` | `<bucket>/status/*` | the dashboard reads status JSON |
| **B** | `oss:ListObjects` | `<bucket>`, conditioned on `status/` | the dashboard enumerates them |

Nobody gets `oss:DeleteObject`. Overwrite protection is header-side
(`x-oss-forbid-overwrite`), so it needs no bucket configuration. Key A cannot
read the status subtree; key B cannot read recordings or write anything.

The device and the app deliberately **share** key A — the key typed into the
app's Cloud upload screen is sent to the device and mirrored into the phone —
which is why it is the union of both rows and not write-only.

> **The two keys it prints are long-lived secrets in plaintext.** Treat the
> output like a written-down password, and prefer `--json` (written mode `0600`)
> to copy/pasting them through chat.

## Other clouds

The permission model, not the script, is the portable part: the same two-key
split applies to AWS S3 and Tencent COS, which devices address through the same
endpoint-derived signing. Only the provisioning API calls here are OSS-specific.

## Tests

```bash
python -m pytest tools/oss-setup -q     # or `make tools-test` from the repo root
```

The suite runs the entire flow — signing included — offline through an injected
transport, so it needs no Aliyun account and no network. Its signing cases mirror
the firmware's own OSS-V4 suite on purpose: a key this script provisions must
verify identically on a device, so the two constructions cannot be allowed to
drift.
