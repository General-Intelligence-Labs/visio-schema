# Fleet Status dashboard

A single self-contained HTML page that shows the fleet: which devices are alive,
what they can see, how hot they are, how full their cards are.

Devices with status reporting enabled periodically `PUT` one JSON object per
device per interval to your own S3 or Aliyun OSS bucket. This page reads that
prefix and renders it. Nothing else runs — no server, no build step, no
dependencies, and no component of ours sits between your devices and your bucket.

```
device ──PUT──▶ s3://BUCKET/status/<YYYY-MM-DD>/<HH>/<MMSS>-<uid>.json
                                       │
                             index.html (LIST + GET, signed in-browser)
```

## Running it

Open `index.html` in a browser and fill in the settings panel: endpoint, region,
bucket, status prefix, and a **read-only** access key.

Because the page talks to your bucket from the browser, the bucket needs a CORS
rule naming the origin the page is served from. A double-clicked file reports its
origin as the literal string `null`:

```bash
# Aliyun OSS
cat > cors.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<CORSConfiguration>
  <CORSRule>
    <AllowedOrigin>null</AllowedOrigin>
    <AllowedMethod>GET</AllowedMethod>
    <AllowedMethod>HEAD</AllowedMethod>
    <AllowedHeader>*</AllowedHeader>
    <ExposeHeader>ETag</ExposeHeader>
    <ExposeHeader>Date</ExposeHeader>
  </CORSRule>
</CORSConfiguration>
EOF
aliyun oss cors --method put oss://BUCKET cors.xml

# AWS S3
aws s3api put-bucket-cors --bucket BUCKET --cors-configuration '{
  "CORSRules": [{
    "AllowedOrigins": ["null"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag", "Date"]
  }]
}'
```

`null` is not a specific origin — it also covers sandboxed iframes and `data:`
URLs. It does **not** expose your data (every request is still signed, and the
bucket stays private), but if you host the page somewhere permanent, name that
origin instead and drop `null`.

CORS is a browser rule, not an access-control one. The credential you paste is
what actually authorises the read, so give the page a key scoped to
`ListBucket` + `GetObject` on the status prefix and nothing more.

## Cost

Keys are chronological and immutable, which is what keeps this cheap: the page
polls with `ListObjectsV2` + `start-after`, so each refresh issues **one** LIST
regardless of fleet size, and downloads each report exactly once. The footer
shows the running request count.

## The report format — `gilabs.status_report/1`

One JSON object per report. Unavailable readings are `null`, never a substituted
value, so a dead sensor never renders as a healthy zero.

```json
{ "schema": "gilabs.status_report/1",
  "device_uid": "0011223344556677", "device_label": "GILABS-AABBCCDD",
  "equipment": "ego", "design_version": "<board design>",
  "firmware_version": "1.0.7",
  "seq": 412, "captured_at_us": 1785053700000000, "clock_synced": true,
  "uptime_s": 12345, "interval_s": 300,
  "recording": { "active": true, "session": "session_00042-1785053000",
                 "started_at_us": 1785053000000000 },
  "disk":    { "free_pct": 62, "no_sdcard": false },
  "thermal": { "soc_c": 71.2, "sensor_c": [58.4, null] },
  "cpu":     { "busy_pct": 41.3 },
  "wifi":    { "state": 1, "ssid": "site-2g", "ip": "192.168.1.44" },
  "upload":  { "pending": 3, "in_flight": 1, "failed": 0, "last_error": "",
               "last_completed_at_us": 1785053400000000,
               "auto_upload_enabled": true },
  "cameras": [ { "index": 0, "frames": 8421330, "last_frame_age_us": 33210 } ],
  "image":   { "format": "jpeg", "width": 640, "height": 360,
               "bytes": 21874, "data_base64": "/9j/4AAQ…" } }
```

Notes for anything consuming this directly:

- **`image` is `null` when there is no picture**, and a sibling `image_reason`
  says why: `not_recording` (the default policy captures only while recording),
  `disabled`, `timeout`, `unavailable`, `encode_failed`. The distinction matters —
  an idle device and a broken camera are not the same condition.
- **Judge freshness from the object's `LastModified`, not `captured_at_us`.** The
  server timestamp is authoritative; a device whose own clock is wrong still
  reports, and says so via `clock_synced: false`.
- **`interval_s` is the device's own reported cadence**, so "late" should be
  measured against it rather than a fixed threshold — a 60-second device and a
  5-minute device are not comparably late at the same age.
- Keys sort chronologically because time precedes the device id. Preserve that if
  you write your own consumer; it is what makes an incremental cursor possible.

## Going beyond a dashboard

For alerting, per-device history, or joins against your own inventory, don't poll
the bucket — subscribe to it. An S3 Event Notification or OSS event trigger into
a function that writes one row per report gives you real queries, with the bucket
remaining the durable log. Detecting *absence* ("device X stopped reporting")
always needs a scheduled sweep; no push notification can tell you about a message
that never arrived.
