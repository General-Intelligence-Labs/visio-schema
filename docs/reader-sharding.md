# On-demand MCAP sessions

`visio_schema.reader.OnDemandSession` reads recordings without keeping every MCAP
part on local disk. Storage access and caching belong to the caller, not this library.

## Build and read

For local files:

```python
from pathlib import Path
from visio_schema.reader import ShardCatalogBuilder

builder = ShardCatalogBuilder()
for path in Path('/recording').glob('*.mcap'):
    builder.index_local_shard(path.name, path)
catalog = builder.build()
```

For remote files, supply these two interfaces:

- `BlobReader`: one object's `size` and `read_at(offset, size) -> bytes`.
  Implement this interface using your storage client. It is a protocol, not a
  client you can instantiate. One reader identifies one file; multiple readers
  can share a storage connection. `BlobReaderIO` adapts it to MCAP's seekable file API.
- `ShardProvider`: `acquire(ShardRef) -> ContextManager[Path]` pins a complete local
  file until context exit; `prefetch(ShardRef)` is optional for the current reader.
  The implementation owns downloading, consistency checks, caching and eviction.

With a `shard: ShardRef`, its `reader: BlobReader`, and a `provider: ShardProvider`
constructed by the caller:

```python
from visio_schema.reader import OnDemandSession, ShardCatalogBuilder

builder = ShardCatalogBuilder()
builder.index_remote_shard(shard, reader=reader, provider=provider)
# Repeat indexing for the remaining shards before build().
catalog = builder.build()
session = OnDemandSession(catalog, provider=provider)
```

Index methods perform I/O, retain entries internally, and return `None`.
Remote indexing reads the summary/metadata indexes, not the whole recording.
Only `SummaryUnavailableError` triggers a warning-logged acquire and local scan
of that one shard. Network/permission/short-read errors and invalid head magic
remain fatal. A scan does not manufacture chunk indexes or repair the source.
After `build()`, further indexing is refused; repeated `build()` is allowed.

## Sampling

```python
from contextlib import closing

with closing(session.keyframe_stream('/ego/camera/0', every_n=2)) as frames:
    for frame in frames:
        process(frame)
```

`keyframe_stream()` decodes images and pins one part at a time. `every_n=2` selects
keyframes 0, 2, 4, ... across part boundaries, not every two video frames or a fixed
time interval. `keyframe_cadence()` measures keyframe spacing to help choose `every_n`.

`keyframe_records(topics, start_ns=..., end_ns=...)` instead returns **compressed**
keyframes in a half-open time window. It pins overlapping parts, orders records by
timestamp and removes identical seam duplicates; conflicting duplicates raise.
It requires chunk indexes. Close partially consumed generators to release all pins.
For per-part checks, use `with session.part_session(part) as local_session`, where
`part` comes from `session.stream_summaries()[0].files`; its path is a logical shard
key, not an unpinned local filename.

The catalog contains `version` and ordered `shards`. Entries reuse `TopicInfo`
and may carry `ShardRotation` (recording ID/part number, not spatial orientation).
`IndexSource` distinguishes summary indexing from local scanning. For optional
persistence, use `catalog.to_json()` / `ShardCatalog.from_json()`;
`OnDemandSession.open(path, provider=...)` loads a saved JSON catalog.

Metadata/calibration load from the first healthy part; global time bounds come
from the catalog. `require_index=True` refuses missing summaries;
`require_chunk_indexes()` refuses scan-only parts. Sidecars and full `stream()`
reads are unsupported and raise `UnsupportedOperationError`.
