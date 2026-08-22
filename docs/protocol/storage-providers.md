# Cloud storage providers — canonical spec

This document is the canonical contract for the **customer storage destination**:
the bucket a device uploads recordings and status reports to, and the companion
app lists (and, in phone-record mode, uploads to) directly. If anything in code
disagrees with this document, this document wins.

There is no backend. Devices and the app hold the customer's own credential and
sign their own requests, so the same contract is implemented independently, and
each implementation MUST conform to the table below:

| Implementation | Language | Conforms | Vocabulary (§1.1) |
|---|---|---|---|
| device firmware | C++ | all five clouds | n/a — no UI |
| companion app | TypeScript | all five clouds | yes — config screen + QR review sheet |
| provisioning tools (`python/visio_schema/settings_qr/payload.py`) | Python | all five clouds | yes |
| setup GUI (`visio-setup/src/setup_gui/`) | Python | **Aliyun OSS + AWS S3 only** | **no** |
| firmware-side QR generator (`visio-embedded/scripts/provision/gen_settings_qr.py`) | Python | **Aliyun OSS + Tencent COS + AWS S3 only** | **no** |
| fleet-status dashboard (`tools/fleet-status/index.html`) | browser JS | **Aliyun OSS + AWS S3 only** | **no** |

Three of those are behind, and each for the same reason: a **second copy of
this table** inside the tool. `visio-setup/src/setup_gui/provision.py` carries
a two-row `ENDPOINT_TEMPLATES`, `gen_settings_qr.py` a three-row `PROVIDERS`,
and the dashboard a single `isOssHost` boolean. The fix is to delete those
copies in favour of `visio_schema.settings_qr.payload.PROVIDERS`, which all
three can import, and not to re-type the new rows into each — tracked, not
done. Until then a customer on GCS or Azure can be provisioned only by the app
or by the schema's own generator.

A provisioning tool MAY offer a shorter preset list than five, but it MUST
accept an arbitrary typed endpoint rather than only its own presets — a preset
is a dropdown convenience, and refusing an endpoint it has no row for would
deny a customer a cloud every other implementation supports.

The dashboard is the outlier: it folds signature flavor and bucket addressing
into a single boolean (`isOssHost`) and hardcodes `list-type=2`, so it
structurally cannot express AWS-signing-with-virtual-hosted addressing, and
both flavors it does have are SigV4-shaped, so it cannot express Azure's
`SharedKey` at all. Customers on Tencent COS, Google Cloud Storage or Azure
Blob therefore have working devices and app but no dashboard. Supporting the
first three means giving it the same two axes and the V1 cursor fallback the
others have; Azure additionally needs the separate signature and list dialect
of §3.1 — tracked, not done.

## 1. The wire contract is provider-agnostic

`SetStorage` / `TestStorage` (`proto/visio_schema/v1/control/command.proto`)
carry `endpoint_url`, `region`, `bucket`, `access_key_id`,
`secret_access_key`, `prefix`, `status_prefix` and nothing else. **There is no
provider field, and there must never be one** — every consumer derives the
provider from the endpoint host (§2). Adding a provider enum would create a
second source of truth that can disagree with the URL being dialled.

### 1.1 What the operator is asked for

The wire is provider-agnostic (§1). **The consoles the operator copies these
values out of are not**, so a UI that asks a human for them MUST use the words
that cloud's own console uses. Endpoint shapes are §3 and §3.1; where the
region comes from is "### Region".

| Cloud | `bucket` | `access_key_id` | `secret_access_key` |
|---|---|---|---|
| AWS S3 | Bucket | Access key ID | Secret access key |
| Aliyun OSS | Bucket | AccessKey ID | AccessKey Secret |
| Tencent COS | Bucket name, **APPID suffix included** | **SecretId** | **SecretKey** |
| Google GCS | Bucket | **HMAC access key** (`GOOG1E…`) | **HMAC secret** |
| Azure Blob | **Container** | **Storage account name** | **Account key** (base64) |

Two clouds MAY share a vocabulary — an S3-compatible endpoint (MinIO, R2,
Wasabi) lands on the AWS row and correctly reuses its three words. The rule is
"each row uses its own console's words", not "every row differs".

Labelling all five "Bucket / Access key ID / Secret access key" is not merely
terse, it is **wrong on three of them**, and each way of being wrong ends in an
opaque failure a long way from the field that caused it:

- an operator hunting for a "bucket" in the Azure portal finds no such thing,
  and the account name belongs in **two** places (the endpoint host and
  `access_key_id`) with nothing on a generic screen to say so;
- Tencent's pair is SecretId/SecretKey, and a SecretId pasted into a field
  labelled "Secret access key" is the classic COS 403;
- GCS's own docs lead with a service-account JSON key, which this integration
  cannot use at all (see below) — a field saying "HMAC access key" is the only
  thing that redirects the operator to the right page.

GCS's HMAC keys are created per service account under *Cloud Storage →
Settings → Interoperability*; they are what its S3-compatible XML API
authenticates with. An organization policy that forbids HMAC keys therefore
forbids this integration — there is no fallback to a service-account JSON key,
because that would need OAuth2 token refresh and RSA signing on a device with no
NTP.

**Where the region is not in the host, the UI must not pretend otherwise.** GCS
has to ask, because only the operator knows the bucket's location. Azure must
not ask at all: it signs no region, so a field labelled "Region" collects a
geography that nothing reads and that will not match the account name the wire
field actually ends up carrying. A UI that hides it derives the value from the
endpoint host instead.

This vocabulary is per-row data, not per-screen copy — the app carries it as
`terms` on its provider row, and the QR generator as that row's prompts — so a
new cloud arrives with its own words rather than inheriting S3's. Which
implementations carry it today is the "Vocabulary" column in the
implementations table at the top of this document.

## 2. Provider detection

Case-insensitive match on the endpoint host — a **suffix** on every row but
one:

| Host | Match | Provider |
|---|---|---|
| `.aliyuncs.com` | suffix | `AliyunOss` |
| `.myqcloud.com` | suffix | `TencentCos` |
| `storage.googleapis.com` | **whole host** | `GoogleGcs` |
| `.blob.core.windows.net` | suffix | `AzureBlob` |
| anything else | — | `AwsS3` (plain path-style S3: AWS, MinIO, R2, B2, Wasabi) |

GCS is matched whole and not as a suffix on purpose: its path-style endpoint
is exactly `storage.googleapis.com`, while the virtual-hosted form
`<bucket>.storage.googleapis.com` is a **different addressing mode** that this
row would sign wrongly. A suffix rule would swallow both.

Both new rows are dispatched off the endpoint for the same reason as the rest:
the endpoint the operator types is the only thing that can name the cloud, and
a second source of truth could disagree with the URL being dialled.

**The five fields carry GCS and Azure unchanged** — no wire change, no new
`SetStorage` field, no growth in the sealed-QR envelope (Azure's oversized
account key must ride the sealed one that already exists — §3.1). What they are
CALLED per cloud is §1.1.

A **CNAME custom domain does not match** and falls through to `AwsS3`, which
will fail against OSS/COS. Provision the provider's own endpoint and the bare
region.

## 3. The provider table

Signature flavor and bucket addressing are **independent axes**. With only AWS
and Aliyun they happened to correlate; Tencent COS is the pair that proves they
are separate, so neither may be derived from the other.

Four of the five clouds share these axes. **Azure Blob does not** — it is not an
S3-family API at all, so forcing it into this table would make half the cells
read "n/a"; it has its own §3.1.

| | AWS S3 | Aliyun OSS | Tencent COS | Google GCS |
|---|---|---|---|---|
| signature | `AWS4-HMAC-SHA256` | `OSS4-HMAC-SHA256` | `AWS4-HMAC-SHA256` | `AWS4-HMAC-SHA256` |
| signing-key seed | `AWS4` + secret | `aliyun_v4` + secret | `AWS4` + secret | `AWS4` + secret |
| credential scope | `<day>/<region>/s3/aws4_request` | `<day>/<region>/oss/aliyun_v4_request` | `<day>/<region>/s3/aws4_request` | `<day>/<region>/s3/aws4_request` |
| addressing | path-style | virtual-hosted | virtual-hosted | path-style |
| request host | `<endpoint-host>` | `<bucket>.<endpoint-host>` | `<bucket>.<endpoint-host>` | `storage.googleapis.com` |
| canonical URI | `/<bucket>/<key>` | `/<bucket>/<key>` | `/<key>` | `/<bucket>/<key>` |
| request path | `/<bucket>/<key>` | `/<key>` | `/<key>` | `/<bucket>/<key>` |
| overwrite guard | `If-None-Match: *` | `x-oss-forbid-overwrite: true` | `x-cos-forbid-overwrite: true` | `x-goog-if-generation-match: 0` |
| conflict status | **412** `PreconditionFailed` | **409** | **409** `FileAlreadyExists` | **412** `PreconditionFailed` |
| list version | V2 (`list-type=2`) | V2 (`list-type=2`) | **V1** (`marker`) | **V1** (`marker`) |
| endpoint template | `https://s3.{region}.amazonaws.com` | `https://oss-{region}.aliyuncs.com` | `https://cos.{region}.myqcloud.com` | `https://storage.googleapis.com` |
| example region | `us-east-1` | `cn-hangzhou` | `ap-guangzhou` | `auto` |

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
- **GCS is the AWS row with a different guard.** Its S3-compatible XML API
  verifies an ordinary `AWS4-HMAC-SHA256` signature with service `s3`, so it
  needs no new signer — only the row. Two deliberate differences: the overwrite
  guard is `x-goog-if-generation-match: 0` (GCS does not honour the
  `If-None-Match: *` wildcard), and the endpoint carries **no region**, so the
  credential-scope region comes from the operator's `region` field. `auto` is
  accepted and is the recommended value; a bucket-location string also works.
  V1 listing is used because the XML API documents `marker` and does not
  document `list-type=2` — and V1 is correct everywhere, the same reasoning
  already applied to COS.

### 3.1 Azure Blob

Azure shares neither axis of the table above, so it is specified separately
rather than forced into a column. Implementations dispatch on the same
host-suffix rule (§2); everything below is what the `AzureBlob` branch does.

| | Azure Blob |
|---|---|
| signature | `SharedKey <account>:<base64 HMAC-SHA256>` |
| signing key | the account key, **base64-decoded** (not a derived per-day key) |
| string to sign | VERB, Content-Encoding, Content-Language, **Content-Length**, Content-MD5, Content-Type, Date, If-Modified-Since, If-Match, If-None-Match, If-Unmodified-Since, Range, then CanonicalizedHeaders, then CanonicalizedResource |
| addressing | path-style; the **account** is the host, the **container** is the first path segment |
| request host | `<account>.blob.core.windows.net` |
| request path | `/<container>/<blob>` |
| canonicalized resource | `/<account>/<container>/<blob>` — account-prefixed, unlike every S3-family row, then each query parameter as `\n<lowercased-name>:<value>` in sorted order |
| required headers | `x-ms-date` (an RFC 1123 date), `x-ms-version` (`2021-08-06` or later), and on PUT `x-ms-blob-type: BlockBlob` |
| overwrite guard | `If-None-Match: *` |
| conflict status | **409** `BlobAlreadyExists` |
| list | `GET /<container>?restype=container&comp=list&maxresults=N[&prefix=…][&marker=…]` |
| list response | `<EnumerationResults><Blobs><Blob><Name>…` — **not** `<ListBucketResult>` |
| list cursor | `<NextMarker>`; absent or empty means the last page |
| endpoint template | `https://{account}.blob.core.windows.net` |
| region | unused — Azure does not carry one in the signature |

Five consequences worth stating, because each is a place an S3-shaped
implementation silently does the wrong thing:

- **`Content-Length` is signed.** An implementation MUST therefore know the
  body length before signing, which a signer whose interface takes only a
  payload hash cannot express.
- **An account key is longer than the plaintext field.** Azure prints an
  88-character base64 account key, while `SetStorage.secret_access_key` allows
  63 bytes — `proto/nanopb.options` sizes the firmware's static decode buffer,
  and an oversized string fails `pb_decode`, so the device discards the whole
  Command and answers nothing (the Tencent SecretId failure, one field over).
  An Azure credential MUST therefore ride the sealed envelope
  (`SetStorage.sealed`, 384 bytes), not the plaintext field, unless and until
  that cap is raised.
- **The canonicalized resource is account-prefixed.** Signing `/<container>/<blob>`
  — the path actually requested — yields 403, in the mirror image of the Aliyun
  quirk above, where the signed path carries a bucket the request does not.
- **The list response is a different document.** A parser that looks for
  `<Contents><Key>` finds nothing and reports an empty bucket rather than an
  error, so a wrong dialect reads as "the customer has no recordings".
- **`x-ms-date` is checked against Azure's clock** with a ~15-minute window.
  A device with no NTP therefore MUST correct its clock from the response
  `Date:` header and re-sign, which on the S3-family rows is merely defensive.

Azure has **no per-operation grant** as fine as the Aliyun RAM / AWS IAM /
Tencent CAM rows in §4: the account key is all-or-nothing over the account. A
customer who needs least privilege should mint a **container-scoped SAS** with
create+list and supply that instead — which the wire cannot carry today, since
`secret_access_key` is interpreted as an account key. Stated rather than hidden;
if a customer asks for it, that is the change to make.

### Region

Derived from the endpoint host where the host is a known form, otherwise taken
from the operator's `region` field:

| Pattern | Region |
|---|---|
| `oss-<region>[-internal].aliyuncs.com` | `<region>` |
| `[<bucket>.]cos.<region>.myqcloud.com` | `<region>` |
| `s3.amazonaws.com` | `us-east-1` |
| `s3[.-]<region>.amazonaws.com` | `<region>` |
| `storage.googleapis.com` | none — the operator's field, defaulting to `auto` |
| `<account>.blob.core.windows.net` | none — Azure signs no region |

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

| Role | Why | Aliyun RAM | AWS IAM | Tencent CAM | Google IAM | Azure |
|---|---|---|---|---|---|---|
| upload | device uploads recordings + status reports; app uploads in phone-record mode | `oss:PutObject` | `s3:PutObject` | `name/cos:PutObject` | `storage.objects.create` | account key (see §3.1) |
| list | the app's cloud recordings list AND the app's Test button (the device probes with a `max-keys=1` list) | `oss:ListObjects` | `s3:ListBucket` | `name/cos:GetBucket` | `storage.objects.list` | account key (see §3.1) |

The two Google permissions are exactly the `roles/storage.objectCreator` +
`roles/storage.legacyBucketReader` pair, or a custom role holding just them; the
HMAC key must belong to the service account that has them.

A second, **read-only** key serves the fleet-status dashboard over the status
subtree only:

| Role | Aliyun RAM | AWS IAM | Tencent CAM | Google IAM | Azure |
|---|---|---|---|---|---|
| read status | `oss:GetObject` | `s3:GetObject` | `name/cos:GetObject` | `storage.objects.get` | — |
| list status | `oss:ListObjects` | `s3:ListBucket` | `name/cos:GetBucket` | `storage.objects.list` | — |

The dashboard reads GCS and Azure for nobody today (§1), so their read-only keys
are unspecified rather than merely unbuilt.

Resource identifiers:

| | Form |
|---|---|
| Aliyun | `acs:oss:*:*:<bucket>/<prefix>/*` (list: `acs:oss:*:*:<bucket>` + an `oss:Prefix` condition) |
| AWS | `arn:aws:s3:::<bucket>/<prefix>/*` (list: `arn:aws:s3:::<bucket>` + an `s3:prefix` condition) |
| Tencent | `qcs::cos:<region>:uid/<APPID>:<bucket>-<APPID>/<prefix>/*` |
| Google | `projects/_/buckets/<bucket>` — GCS IAM binds at the BUCKET, with no prefix condition, so a Google customer's key reaches the whole bucket however the prefix is set |
| Azure | none — the account key is scoped to the account (§3.1) |

**Nobody gets a delete or a bucket-info grant**, and no implementation may add an
operation that would need one — in particular the storage probe must stay a list
and must not become a HEAD or GET. Google and Azure cannot honour the *prefix*
half of that (their grants do not take a prefix condition), which is a real
widening for those two customers and is why the table above says so out loud
rather than implying parity. The Aliyun RAM policy above is scriptable end
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

Two additions for GCS and Azure, because the device's probe cannot
catch either: the probe is a ONE-page list, so a wrong list dialect or cursor
still answers it. Paginate past one page in the app against a real GCS bucket
and a real Azure container — that is where the V1 `marker` fallback and the
`<EnumerationResults>` parse actually get exercised.
