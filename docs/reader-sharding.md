# On-demand MCAP sessions

`visio_schema.reader.sharding` provides an advanced/internal reader API for recordings
whose MCAP parts cannot all fit on local disk. It does not implement storage access,
credentials, downloads, caching, or IPC. The package-root facade is unchanged.

## Storage boundary

The caller supplies two structural protocols:

- `BlobReader`: `size: int` and `read_at(offset, size) -> bytes`. Reads must return the
  requested range exactly; the adapter handles EOF. Transport/permission/short-read
  errors propagate, rather than being interpreted as damaged recordings.
- `ShardProvider`: `acquire(ShardRef(key, size)) -> ContextManager[Path]` and
  `prefetch(ShardRef) -> None`. The context pins a complete, seekable local MCAP until
  exit. Provider implementations own downloads, consistency checks, refcounts, disk
  admission, eviction, and task cleanup. Prefetch is optional for the reader's current
  implementation; reads do not require it.

`BlobReaderIO` implements a seekable file interface. `ShardCatalogBuilder` wraps it
once in a standard buffered reader (8 KiB read-ahead) so the MCAP parser's small
field reads do not each require a remote request. MCAP decides the seeks/ranges;
neither the provider nor the catalog builder embeds footer/summary offsets.

## Build a catalog

```python
import json
from pathlib import Path

from visio_schema.reader import ShardCatalogBuilder, SummaryUnavailableError

builder = ShardCatalogBuilder(
    episode_id='episode-id', source_uri='storage://recording/', created_by='my-driver/1'
)
entries = []
for ref, blob in remote_parts:
    try:
        entry = builder.entry_from_blob(ref.key, blob)
    except SummaryUnavailableError:
        with provider.acquire(ref) as local_path:
            entry = builder.entry_from_local_file(ref.key, local_path)
    entries.append(entry)
catalog = builder.build(entries)
Path('catalog.json').write_text(json.dumps(catalog.to_json()), encoding='utf-8')
```

`SummaryUnavailableError` alone signals an absent/unreadable summary for which the
caller may explicitly download and scan one local part. Invalid head magic is a
fatal non-MCAP input, not a fallback request. A local fallback reuses the ordinary
`Session` index/truncation logic. A scan never manufactures chunk indexes or marks
an unreadable tail as intact. No implicit reindexing or upload occurs.

The version-1 JSON contains `version`, `episode_id`, `source_uri`, `created_by`, and
`shards`. Each shard contains:

- `key`, `size`, `start_ns`, `end_ns` (inclusive message bounds, nullable);
- `chunk_indexed`, `truncated`, `index_source` (`range_summary` or `download_scan`);
- `topic_info`: objects with `topic`, `schema_name`, `messages`, plus shard-level
  `messages` for accurate topic/file inventories;
- `metadata_names` (array, or null when unknown after a scan);
- `rotation`: `{recording_id, part_index, video_continuation}`, or null.

Storage-specific fields such as etag are not part of the catalog; unknown JSON
fields and unknown versions are rejected. `build()` shares ordinary `Session`'s
ordering: recording/part identity takes priority for rotated recordings, whose
later parts may contain earlier retained backlog; other files use start time.
Empty/unknown bounds stay unknown, rather than being invented from filenames.

## Read with bounded local materialization

```python
from contextlib import closing

from visio_schema.reader import OnDemandSession

session = OnDemandSession.open('catalog.json', provider=provider)
print(session.metadata, session.topics())
with closing(session.keyframe_stream('/ego/camera/0', every_n=2)) as frames:
    for frame in frames:
        consume(frame)
```

The initial metadata and calibration come from the first non-truncated part (or
first recoverable part if all are truncated), acquired and released lazily. Session
bounds come from the complete catalog; inventories require no downloads. The
resolved device prefix is shared across per-part reads.

`keyframe_stream()` pins one part at a time. Its keyframe counter spans part
boundaries; a `[start_ns, end_ns)` window anchors the counter at the window, exactly
as in ordinary `Session`. Decode buffers/decoders remain part-scoped. Close a
partially consumed generator to release its current pin immediately.

`keyframe_records(topics, start_ns=..., end_ns=...)` pins only window-overlapping
parts and delegates to ordinary `Session` for ordered, deduplicated compressed
keyframes. Closing the generator releases **all** pins, including on errors.
`require_chunk_indexes()` refuses scan-only/unindexed parts without downloading;
`require_index=True` also refuses missing/unreadable summaries at construction.
`sparse_read_stats` accumulates delegated reads when each query finishes/closes.

For sampled-part calibration checks, use the same API on either session type:

```python
for part in session.stream_summaries()[0].files:
    with session.part_session(part) as local_session:
        local_session.require_chunk_indexes()
        check_calibration(local_session.calibration)
```

On-demand inventory `FileSummary.path` is the logical shard key, **not** an
unpinned local pathname. Only `part_session()` resolves it through the provider.
It uses strict indexed construction; sampled-part calibration consistency remains
the caller's responsibility.

The first implementation intentionally does not support sidecars or full
`stream()` reads; these raise `UnsupportedOperationError`, never download the
whole episode silently. `BaseSession` is only the common structural metadata,
calibration, device, topic, and inventory protocol, not a promise of every ordinary
`Session` operation.
