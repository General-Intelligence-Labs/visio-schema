# Cloud storage providers — canonical spec

This document is the canonical contract for the **customer storage destination**:
the bucket a device uploads recordings and status reports to, and the companion
app lists (and, in phone-record mode, uploads to) directly. If anything in code
disagrees with this document, this document wins.

There is no backend. Devices and the app hold the customer's own credential and
sign their own requests, so the same contract is implemented independently, and
each implementation MUST conform to the table below:

| Implementation | Language | Conforms |
|---|---|---|
| device firmware | C++ | all three clouds |
| companion app | TypeScript | all three clouds |
| provisioning tools | Python | all three clouds |
| fleet-status dashboard (`tools/fleet-status/index.html`) | browser JS | **Aliyun OSS + AWS S3 only** |

The dashboard is the outlier: it folds signature flavor and bucket addressing
into a single boolean (`isOssHost`) and hardcodes `list-type=2`, so it
structurally cannot express AWS-signing-with-virtual-hosted addressing. A COS
customer therefore has working devices and app but no dashboard. Supporting it
means giving it the same two axes and the V1 cursor fallback the other three
have — tracked, not done.

## 1. The wire contract is provider-agnostic

`SetStorage` / `TestStorage` (`proto/visio_schema/v1/control/command.proto`)
carry `endpoint_url`, `region`, `bucket`, `access_key_id`,
`secret_access_key`, `prefix`, `status_prefix` and nothing else. **There is no
provider field, and there must never be one** — every consumer derives the
provider from the endpoint host (§2). Adding a provider enum would create a
second source of truth that can disagree with the URL being dialled.

## 2. Provider detection

Case-insensitive **suffix** match on the endpoint host:

| Host suffix | Provider |
|---|---|
| `.aliyuncs.com` | `AliyunOss` |
| `.myqcloud.com` | `TencentCos` |
| anything else | `AwsS3` (plain path-style S3: AWS, MinIO, R2, B2, Wasabi) |

A **CNAME custom domain does not match** and falls through to `AwsS3`, which
will fail against OSS/COS. Provision the provider's own endpoint and the bare
region.

## 3. The provider table

Signature flavor and bucket addressing are **independent axes**. With only AWS
and Aliyun they happened to correlate; Tencent COS is the pair that proves they
are separate, so neither may be derived from the other.

| | AWS S3 | Aliyun OSS | Tencent COS |
|---|---|---|---|
| signature | `AWS4-HMAC-SHA256` | `OSS4-HMAC-SHA256` | `AWS4-HMAC-SHA256` |
| signing-key seed | `AWS4` + secret | `aliyun_v4` + secret | `AWS4` + secret |
| credential scope | `<day>/<region>/s3/aws4_request` | `<day>/<region>/oss/aliyun_v4_request` | `<day>/<region>/s3/aws4_request` |
| addressing | path-style | virtual-hosted | virtual-hosted |
| request host | `<endpoint-host>` | `<bucket>.<endpoint-host>` | `<bucket>.<endpoint-host>` |
| canonical URI | `/<bucket>/<key>` | `/<bucket>/<key>` | `/<key>` |
| request path | `/<bucket>/<key>` | `/<key>` | `/<key>` |
| overwrite guard | `If-None-Match: *` | `x-oss-forbid-overwrite: true` | `x-cos-forbid-overwrite: true` |
| conflict status | **412** `PreconditionFailed` | **409** | **409** `FileAlreadyExists` |
| list version | V2 (`list-type=2`) | V2 (`list-type=2`) | **V1** (`marker`) |
| endpoint template | `https://s3.{region}.amazonaws.com` | `https://oss-{region}.aliyuncs.com` | `https://cos.{region}.myqcloud.com` |
| example region | `us-east-1` | `cn-hangzhou` | `ap-guangzhou` |

Notes on the two rows that most often get implemented wrong:

- **Aliyun signs a path it does not request.** OSS requires virtual-hosted
  addressing (path-style answers 403 `SecondLevelDomainForbidden`), but its V4
  canonical URI still carries `/<bucket>/<key>`. Signing `/<key>` yields
  `SignatureDoesNotMatch`. Verified against OSS's echoed `CanonicalRequest`.
  Tencent does **not** share this quirk — it is an ordinary AWS virtual-hosted
  signature, so signed path and request path are identical.
- **Aliyun folds default-signed headers it merely receives.** OSS adds
  `content-type`, `content-md5` and any `x-oss-*` header the client sent into the
  canonical request when verifying, *even for a presigned URL*. Anything in that
  set must be signed with a fixed value and sent verbatim, or the request 403s.
  AWS/COS SigV4 verify only the headers named in `SignedHeaders`.

### Region

Derived from the endpoint host where the host is a known form, otherwise taken
from the operator's `region` field:

| Pattern | Region |
|---|---|
| `oss-<region>[-internal].aliyuncs.com` | `<region>` |
| `[<bucket>.]cos.<region>.myqcloud.com` | `<region>` |
| `s3.amazonaws.com` | `us-east-1` |
| `s3[.-]<region>.amazonaws.com` | `<region>` |

### Bucket names

Tencent COS bucket names **include the APPID suffix** — `recordings-1250000000`,
not `recordings`. The console shows the name and the APPID separately, so
entering the bare name is the single likeliest misconfiguration; it presents as
HTTP 404 `NoSuchBucket`, which looks nothing like a naming problem. Implementations
SHOULD name the APPID rule in that error. Nothing else in the system needs to know
about APPIDs — the operator types the full name and it is used verbatim.

### Overwrite protection ("put will not override")

Object keys are unique by construction (`<prefix>/<serial>/session_<NNNNN>-<start>/<part>`),
so a conflict can only be **our own earlier upload whose delete did not land**.
Every PUT therefore carries the provider's overwrite guard, and callers MUST
treat that provider's OWN conflict status as "already uploaded" rather than as
failure:

```
already_uploaded(provider, status) := status == provider.conflict_status
```

**Per provider, not a union.** A caller that accepts both 409 and 412 everywhere
is wrong in a way that destroys data: on a row that never sends `If-None-Match`,
a 412 means something else entirely, and the response to "already uploaded" is
to tombstone the part and delete the local file. The implementations are
`is_already_uploaded_status` (C++) and `isAlreadyUploadedStatus` (TS).

Two limits, to be documented rather than defeated:

- **Bucket versioning silently disables the guard** on both Aliyun and Tencent.
  Versioning (enabled *or* suspended) and forbid-overwrite are an either/or
  choice; with versioning on, a re-PUT creates a new version and returns 200.
- **MinIO/R2 and other S3-compatible servers ignore the `If-None-Match: *`
  wildcard** (they require a concrete ETag), so only real AWS S3 enforces the
  guard on the `AwsS3` branch. Non-AWS S3-compatible hosts keep overwriting.

### Listing

The device's storage test is a `max-keys=1` list confined to the recordings
prefix — an existence-and-permission probe, one page, so the list version is
immaterial there beyond being accepted. The **app** paginates, and that is where
the version matters.

COS's own `GET Bucket (List Objects)` API documents V1 only — `marker` in,
`IsTruncated` + `NextMarker` out — with no `list-type=2` or
`continuation-token`. Whether its S3-compatible layer also accepts V2 is
**unverified**; V1 is used for COS because it is correct either way.

Implementations MUST treat the cursor as opaque and resolve the next one as:

1. `NextContinuationToken` (V2), else
2. `IsTruncated == true` → `NextMarker` if present, else **the last `<Key>` on
   the page**.

Step 2's fallback is load-bearing: V1 omits `NextMarker` unless a delimiter was
used, and these listings use none. Getting it wrong truncates the list at one
page **silently**.

## 4. The permission contract

One credential is shared by the device and the app — the key typed into the
app's Cloud upload screen is pushed to the device *and* mirrored into the phone
— so it is the union of both roles, and deliberately no larger:

| Role | Why | Aliyun RAM | AWS IAM | Tencent CAM |
|---|---|---|---|---|
| upload | device uploads recordings + status reports; app uploads in phone-record mode | `oss:PutObject` | `s3:PutObject` | `name/cos:PutObject` |
| list | the app's cloud recordings list AND the app's Test button (the device probes with a `max-keys=1` list) | `oss:ListObjects` | `s3:ListBucket` | `name/cos:GetBucket` |

A second, **read-only** key serves the fleet-status dashboard over the status
subtree only:

| Role | Aliyun RAM | AWS IAM | Tencent CAM |
|---|---|---|---|
| read status | `oss:GetObject` | `s3:GetObject` | `name/cos:GetObject` |
| list status | `oss:ListObjects` | `s3:ListBucket` | `name/cos:GetBucket` |

Resource identifiers:

| | Form |
|---|---|
| Aliyun | `acs:oss:*:*:<bucket>/<prefix>/*` (list: `acs:oss:*:*:<bucket>` + an `oss:Prefix` condition) |
| AWS | `arn:aws:s3:::<bucket>/<prefix>/*` (list: `arn:aws:s3:::<bucket>` + an `s3:prefix` condition) |
| Tencent | `qcs::cos:<region>:uid/<APPID>:<bucket>-<APPID>/<prefix>/*` |

**Nobody gets a delete or a bucket-info grant**, and no implementation may add an
operation that would need one — in particular the storage probe must stay a list
and must not become a HEAD or GET. The Aliyun RAM policy above is scriptable end
to end; AWS and Tencent are configured by hand from the tables above.

## 5. Conformance

Each implementation — device firmware, companion app (including its handoff to
the native uploaders) and the provisioning tools — pins this table in its own
test suite. Nothing yet checks the C++, TS and Python tables against **each other** — they
agree by review. A shared golden table next to this document, pinned by each
implementation, is the obvious fix if they ever drift.

A green test suite proves the *shape* only. Signature and addressing faults
surface exclusively against a live bucket, so any change here needs a real
round-trip per affected provider: Test button → 200, upload → object present,
re-upload → conflict status treated as already-uploaded.
