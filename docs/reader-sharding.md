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

For remote files, implement one `ShardProvider` using your storage client:

- `list_shards() -> Sequence[ShardRef]` describes this input's objects and expected sizes.
- `read_at(shard, offset, size) -> bytes` returns exactly the requested range.
- `acquire(shard) -> ContextManager[Path]` pins a complete local file until context exit.
- `prefetch(shard)` permits optional preparation; the current reader does not call it.

`ShardRef(key, size)` identifies the object and its expected length. The provider
owns downloads, consistency checks, caching and eviction. The library internally
adapts provider ranges to MCAP's seekable file API; callers do not create readers.

With a `provider: ShardProvider` constructed by the caller:

```python
from visio_schema.reader import OnDemandSession, ShardCatalogBuilder

catalog = ShardCatalogBuilder.from_provider(provider)
session = OnDemandSession(catalog, provider=provider)
```

`from_provider()` lists shards once, indexes them with `index_remote_shard()`,
and returns an ordered in-memory catalog. `OnDemandSession` uses that catalog;
it does not list shards again. An empty listing produces an empty catalog, which
cannot be opened as a session. For low-level control, use
`builder.index_remote_shard(shard, provider=provider)` before `builder.build()`.
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
