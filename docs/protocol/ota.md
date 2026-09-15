# Visio OTA — the update contract

How firmware reaches a Visio device: the frames on the wire, the states the
device answers with, the container a multi-board device is sent, and the rules
a sender must follow. `visio_schema.wire.ota` (Python) and
`visio_schema/wire/ota.hpp` (C++) are the reference implementations, and
`tests/golden/ota_vectors.txt` pins them to each other.

**One package, one device.** A sender pushes ONE file to ONE device and is
agnostic to what is behind it. An Ego 1 is a single board; an Ego Pro head is a
board with limbs attached, and it is sent exactly the same way — it unpacks
what it received, updates its own limbs, and reports one verdict. Nothing above
a device plans a per-board delivery, and the shape recurses: a device drives its
children exactly as a sender drives it.

> **Normative vs non-normative.** §1–§5 are the contract: implementations MUST
> conform, and a change there is a MAJOR bump under
> [`versioning.md`](versioning.md). §6 is TUNING — timers, window sizes and the
> retry cadence. Those are what the reference implementations happen to use;
> they are deliberately outside the contract, because a stall timeout that
> needed a major version bump to raise is a stall timeout nobody can raise.

## 1. Streams and addressing

```
host → device   CONTROL_STREAM_OTA = 5      OtaMessage{begin|chunk|commit|abort|query}
device → host   /<device>/ota_status        OtaStatus
```

`OtaMessage` is on a reserved control id, so its id is identical on every link
and is never remapped — which means the id cannot say WHICH device a frame is
for. `OtaMessage.target_device` (the device's announced `DeviceInfo.device_name`)
does. A receiver MUST ignore an `OtaMessage` whose `target_device` is neither
empty nor its own name.

`ota_status` is a named dynamic channel announced in `DeviceInfo`, not a
reserved id. A sender MUST learn its stream id from the announce; there is no
compile-time value.

**A control id is not relayed by the bus.** Forwarding one is an application
decision, made by the device that has children. What this means in practice is
that a sender talks to the device it is connected to, and that device talks to
its own children on its own links.

## 2. The session

```
  → OtaQuery                    (optional; see §3)
  ← OtaStatus{IDLE, max_chunk_bytes}
  → OtaBegin{fw_version, board, total_bytes, chunk_bytes}
  → OtaChunk{offset, data}      ... windowed against the acks
  ← OtaStatus{RECEIVING, bytes_received}
  → OtaCommit
  ← OtaStatus{STAGED}           then the device reboots to apply
```

`session_id` correlates every frame of one transfer and is echoed in every
`OtaStatus`. A sender MUST ignore a status carrying a DIFFERENT non-zero
session: a device that reboots mid-update publishes its own terminal verdict
under a session the sender never minted. A status with `session_id == 0` is
NOT foreign — it is a device that does not attribute its status — and MUST be
honoured.

**`bytes_received` is the sender's window credit**, and it is CONTIGUOUS: it is
the high-water mark of bytes the device has durably accepted with no gap below
it, never a count of bytes that arrived. A sender MUST NOT commit before the
device has acked every byte.

**`NEEDS_RESUME` means rewind**, not fail: the sender seeks to `bytes_received`
and resends from there. A link that drops frames is the normal case on CDC-ACM,
and a transfer that restarted from zero on each one would never finish.

**A commit's `STAGED` ack routinely races the reboot that applies it.** A sender
MUST NOT require it. Waiting briefly for an explicit `SUCCESS` or `FAILED` and
otherwise assuming the device staged is the correct behaviour; the proof of
success is the version the device announces after it comes back, which a sender
reads from `DeviceInfo` — never from the transfer ending well.

**Payload opacity.** The bytes are one opaque encrypted image and the sender is
a BLIND RELAY: it moves a file it cannot read, and only the device holds the
key. Authenticity rests on that shared key, which is why no field here carries
a signature.

## 3. Chunk-size negotiation

`OtaChunk.data` is a nanopb bytes field on the device side, and nanopb is built
without `PB_FIELD_32BIT` — `pb_size_t` is 16-bit. A chunk past that limit does
not fail cleanly: the device answers EVERY frame with a decode error, which
reads like a dead link rather than a size problem.

A sender SHOULD ask before it declares, by sending `OtaQuery` before `OtaBegin`
and reading `OtaStatus.max_chunk_bytes`. The rules, in order:

1. **No advert (`0`) or no answer** means firmware predating the field. The
   sender keeps its own default UNTOUCHED.
2. **An advert WINS over that default, in EITHER direction.** Taking
   `min(ours, advertised)` would make the field a no-op for every device that
   can take more than a conservative default, which is most of them.
3. **...but only within `[8 KiB, 60 KiB]`.** The ceiling is the nanopb limit
   with room for the fields wrapping the payload. The floor mirrors the
   device's own ack interval: a device paces its `RECEIVING` acks off
   `OtaBegin.chunk_bytes`, and those acks are the sender's window credit, so a
   tiny chunk starves the window rather than making the transfer safer.
4. **A LINK cap applies last, to everything.** The CDC-ACM gadget's RX FIFO is
   a property of the cable, which the device cannot see and so cannot report —
   the same board reached over USB-NCM and over CDC-ACM advertises the same
   number and only one of those can take it. A cap is therefore never overruled
   by an advert, which is what distinguishes it from the default in rule 1.

When one `OtaBegin` reaches several devices, the sender takes the SMALLEST
advert across everyone who answers: one image means one chunk size, and it must
fit the most constrained board.

`advert_above_ceiling` and `advert_below_floor` in `tests/golden/ota_vectors.txt`
pin rules 3 and 4 across implementations.

## 4. The board mark

`OtaBegin.board` is the board the IMAGE was published for — its
`design_version`, the same string the device announces as
`DeviceInfo.hardware_revision`. A device refuses a mismatch as `wrong_board`.

A sender MUST stamp it from where the image came from, never from what it read
off the device: sourcing it from the device makes the device's own check a
tautology. An empty mark is the legacy unmarked push and is byte-identical to
the pre-mark encoding (proto3 omits an empty string), so the check simply does
not fire — a device may be configured to require the mark instead.

## 5. The package (multi-board devices)

A device with children is sent ONE file containing every board's image. It is an
uncompressed (stored) **ustar** archive:

```
index.txt           MUST be member 0
<hwrev>.img         one image per board, verbatim
...                 the RECEIVING device's own image LAST
```

`index.txt` is `key=value` lines — not JSON, because a device has to parse it
and a JSON parser would be the largest dependency in this library for the sake
of ~300 bytes:

```
v=1
product=ego_pro
version=1.3.0
board=<role>:<hwrev>:<equipment>:<soc>:<file>:<bytes>:<sha256>
hmac=<hex>
```

Four rules, each load-bearing:

- **Index first.** Tar has no table of contents, so a reader discovers members
  as it goes — and a device must validate the WHOLE roster before it erases
  anything. A reader MUST hard-fail if member 0 is not `index.txt`.
- **Self last.** By the time the receiving device's own image arrives, every
  refusal the index can produce has already fired; what is left is a write
  failure, and discovering one before the inactive slot is erased is strictly
  better than after. This is BYTE order in the archive. It is not apply order.
- **Children first is APPLY order**, and separate: the coordinator stays on
  known-good firmware through the risky part.
- **The index is authenticated** — HMAC-SHA256 over the index body under the
  same key the images are encrypted with. Every image is independently
  key-encrypted and self-digesting, so an attacker cannot forge one; but they
  could RELABEL which board an image is for, and the device would then hand a
  genuine image to the wrong board. An image for the wrong SoC is a
  maskrom-recovery brick.

A device whose attached child has no image in the package MUST refuse the whole
package, naming that board, and MUST do so before anything is written. Half
updating defeats the one-product-version rule the transaction exists to enforce.
A child that is NOT attached is simply not in the roster and is not an error.

A stream whose first bytes are a bare image rather than a tar is an implicit
package of one: board from `OtaBegin.board`, destination self. That is what
keeps a single-board device's contract unchanged.

## 6. Tuning — NOT normative

These are the reference implementations' numbers. A conforming sender may use
others; none of them is part of the wire contract and changing one is not a
version bump.

| | Value | Why |
|---|---|---|
| default chunk | 32 KiB | what every fielded device already accepts |
| CDC-ACM chunk / window | 4 KiB / 8 KiB | in-flight bytes must stay inside the gadget RX FIFO |
| TCP chunk / window | 32 KiB / 2 MiB | the socket buffer absorbs a device's flash-write stall |
| stall timeout | 45 s | no contiguous-ack progress → abort |
| soft retry | 6 s | quiet tail → re-drive from the last ack |
| commit wait | 8 s | how long to wait for a `STAGED` that may be racing the reboot |
| query wait | 1.5 s | how long to collect `max_chunk_bytes` answers |
| whole-transfer deadline | 900 s | the stall timer alone cannot catch a device trickling one ack just under it |

## Conformance

`tests/golden/ota_vectors.txt` is a TRANSCRIPT, not a codec vector: per case,
the ordered `OtaMessage`s a driver must emit against a scripted device, and the
outcome it must reach. It pins sends and the outcome, never the recv call
pattern — pinning that would freeze an implementation detail and make an async
driver unwritable.

Conformance is not equivalence. The transcript cannot reach a host's outbox
depth, a CDC-ACM FIFO, or a real flash write. Keep the vector set tight to the
rules stated above rather than growing it into a pseudo-integration suite.
