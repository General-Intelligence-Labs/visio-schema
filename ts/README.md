# `@gilabs/visio-schema`

The Visio wire contract for TypeScript — a **peer of the `visio_schema` Python
package**, not a relocation of an app's `src/lib`. Same organisation by concern,
same facade discipline, same public/internal split.

## What belongs here

> A module belongs in this package iff the contract it implements is owned by
> visio-schema — a `.proto`, or a doc under `docs/protocol/`.

The Python package's module set is evidence for that rule, not the definition of
it. An app's session management is behind no contract and stays in the app; the
topic grammar (`docs/protocol/stream_type_map.md`) and the storage provider
table (`docs/protocol/storage-providers.md`) are contracts and live here, even
where Python happens not to implement one of them.

## Two deliberate asymmetries with the Python package

Python has `transport/` and `mcap/`. This package has **types only** for the
first and **nothing** for the second, because on the phone both are native by
design:

| the app does natively | why | protobuf |
|---|---|---|
| COBS/CRC framing | it is the transport | none |
| demux on `wire.Header` fields 1–3 | video stays native, the rest goes to JS | 3 hand-scanned varints |
| H.265 → MediaCodec / VideoToolbox | video never crosses the RN bridge | `CompressedVideo` field 3 only |
| MCAP writing | phone-side recording of that video | none — a Foxglove channel in `DeviceInfo` carries its serialized `FileDescriptorSet`, which is exactly what an MCAP `Schema` record's `data` expects |

So the native side needs **no protobuf library**, and this package owns payload
decode — the only place bindings were ever needed. The gaps are explainable, not
holes.

## Conformance

`golden/` ships the cross-language corpora, so a consumer conforms without a
sibling checkout. `ota_vectors.txt` pins `OtaMessage` frames and the outcome,
never the recv call pattern — pinning that would freeze an implementation detail
and make an async driver unwritable, which this one is (a synchronous
`recv(timeout)` cannot exist in JS).

What the vectors **cannot** see is anything on another stream — the quiesce is a
`Command`, and `docs/protocol/ota.md` §6 is the only thing standing between a
new implementation and a push that dies on a loaded link. Read it.
