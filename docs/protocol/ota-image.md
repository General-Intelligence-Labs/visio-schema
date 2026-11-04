# OTA image envelopes

An individual release is `VENC`, a 56-byte little-endian prefix followed by
AES-256-CTR ciphertext. A head uses this same ordinary envelope; leaf RKFW
images are embedded once in its OEM filesystem before head compression/encryption.
After reboot the head creates VENC1 envelopes while sending leaves their images.

| Offset | Field |
|---|---|
| 0 | `VENC` (4 bytes) |
| 4 | format (u8): 1 = RKFW, 3 = gzip of RKFW |
| 5 | reserved (3 bytes, zero for format 3) |
| 8 | random nonce (8 bytes) |
| 16 | expanded RKFW length (u64) |
| 24 | expanded RKFW SHA-256 (32 bytes) |

Format 2 remains reserved for signed-envelope work. The CTR IV is the nonce
followed by eight zero bytes. Format 3 uses gzip level 6, mtime=0, BEFORE
encryption. It carries exactly one gzip member with no trailing data.

Receivers decrypt, incrementally expand, and check the expanded length and hash
before committing. Format 3 expansion is bounded to 512 MiB, with bounded output
buffers. Partition capacity and file-staging space checks use expanded lengths;
OTA offsets, progress, and rig member sizes use stored/transmitted lengths.

Only RV1126B publishes/accepts format 3. RV1106 retains format 1. A receiver
predating compression must first receive an ordinary format-1 bridge update.
Factory stations expand and verify either format before invoking the unchanged
Rockchip USB flasher. Recovery continues to consume expanded RKFW images.
