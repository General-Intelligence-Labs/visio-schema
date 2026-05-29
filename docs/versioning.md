# Versioning Policy

`visio-schema` follows semver at the repo level, with `buf` enforcing
wire-compatibility checks at the proto level. This document is short
on purpose — the rules need to be memorable.

## Repo version (semver)

Released as git tags `vMAJOR.MINOR.PATCH`.

| Bump | When |
|---|---|
| **PATCH** | Comment-only changes; new optional fields with new tag numbers; new enum values added at end of category range. Wire-compatible in both directions. |
| **MINOR** | New `StreamKind` values, new top-level message types, new `.proto` files. Wire-compatible: old peers can't understand new types but won't break on them. Per-language bindings get new symbols. |
| **MAJOR** | Removing or renumbering fields, removing enum values, renaming message types, changing units / semantics of existing fields, changing the wire framing spec. Old and new peers do NOT interoperate. |

We are **pre-1.0** while the schema is still being shaped. Once a
production peer fleet exists in the wild, the next breaking change
bumps to `v1.0.0` and after that breaking changes become rare events.

## Per-package versioning (`v1`, `v2`)

Every proto package lives under a `vN` suffix:

```
proto/visio/sensor/v1/imu_raw.proto      package visio.sensor.v1
proto/visio/wire/v1/header.proto         package visio.wire.v1
```

If we ever need to break a single package's wire contract, we add a
**new package alongside the old one**:

```
proto/visio/sensor/v1/imu_raw.proto      stable, frozen
proto/visio/sensor/v2/imu_raw.proto      new shape, new StreamKind value
```

Both coexist for a deprecation window. Old consumers keep reading v1;
new producers emit v2. We never mutate v1 semantics in place — that
would silently corrupt deployed devices.

## buf breaking checks

`make breaking` runs `buf breaking proto --against '.git#branch=main,subdir=proto'`
on every PR. The rule set is `FILE`, which is buf's strictest:

- Field tag numbers cannot change.
- Field types cannot change.
- Enum values cannot be removed or renumbered.
- Message types cannot be removed.
- Required fields cannot be added (proto3 has none, but worth knowing).

A PR that fails `make breaking` MUST either:

1. Revert the breaking change, OR
2. Move the changed type to a new package version (`v2`), leaving the
   `v1` definition untouched, OR
3. Bump the repo MAJOR version and document the break in
   `CHANGELOG.md` (TBD).

`docs/framing.md` and `docs/timesync.md` are **part of the contract**.
Changes to them follow the same semver rules even though buf doesn't
verify them — a wire-format spec change is a major version bump
regardless of whether any `.proto` file moves.

## Foxglove submodule

The submodule is pinned to a specific release tag. Bumps follow
[`foxglove_compat.md`](foxglove_compat.md) section "Bump procedure"
and are minor-version events at the visio-schema level (they add
foxglove fields; protobuf forward-compat absorbs the rest).

## Deprecation

Fields and enum values are deprecated via the `[deprecated = true]`
option, not removed. Removal happens at the next MAJOR bump.

```proto
optional uint64 old_field = 5 [deprecated = true];
```

Deprecated fields stay on the wire for at least one full minor cycle
to give downstream consumers time to migrate.

## Release process (sketch)

1. Open a release PR. Bump version in `VERSION` (TBD; for now the
   `MASTER_PLAN.md` version block).
2. CI runs `make lint`, `make breaking`, `make gen`.
3. Tag `vX.Y.Z` on merge.
4. CI publishes language packages:
   - C++: header-only release artifact
   - Python: wheel to PyPI (`visio-schema` package, abi3)
   - Java, Swift: TBD (post-Phase 2)
5. Downstream `visio-mq` pulls the new tag at its leisure.
