"""Storage-independent catalogs and bounded, provider-backed MCAP sessions."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from mcap.exceptions import McapError
from mcap.reader import make_reader

from .domain import (
    VIDEO_SCHEMA,
    Calibration,
    FileSummary,
    Frame,
    KeyframeCadence,
    Record,
    SessionMeta,
    StreamSummary,
    TopicInfo,
)
from .session import (
    ROTATION_METADATA,
    Session,
    SummaryUnavailableError,
    _detect_device,
    _FileIndex,
    _KeyframeProgress,
    _linear_metadata,
    _rotation_from_metadata,
    _rotation_order,
    _RotationIdentity,
    strip_device_topic_prefix,
)


class UnsupportedOperationError(NotImplementedError):
    """This access path is intentionally unavailable for on-demand sessions."""


class BlobReader(Protocol):
    """Random-access bytes supplied by the caller, without a storage SDK."""

    @property
    def size(self) -> int: ...

    def read_at(self, offset: int, size: int) -> bytes: ...


class BlobReaderIO(io.BufferedIOBase):
    """Adapt one blob's read_at(offset, size) to MCAP's read/seek/tell interface.

    This adapter owns only a byte cursor, not a download or connection. MCAP's
    parser chooses the offsets; an outer BufferedReader coalesces small reads.
    Use the buffered interface without read-ahead: mcap wraps RawIOBase in
    temporary BufferedReaders whose destruction would close the shared source.
    """

    def __init__(self, reader: BlobReader):
        super().__init__()
        if type(reader.size) is not int or reader.size < 0:
            raise ValueError('BlobReader.size must be a nonnegative integer')
        self._reader = reader
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        self._checkClosed()
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        if whence == io.SEEK_CUR:
            offset += self._position
        elif whence == io.SEEK_END:
            offset += self._reader.size
        elif whence != io.SEEK_SET:
            raise ValueError(f'invalid seek whence: {whence}')
        if offset < 0:
            raise McapError('MCAP seek before the start of the blob')
        self._position = offset
        return offset

    def read(self, size: int = -1) -> bytes:
        self._checkClosed()
        remaining = max(0, self._reader.size - self._position)
        size = remaining if size is None or size < 0 else min(size, remaining)
        if not size:
            return b''
        data = self._reader.read_at(self._position, size)
        if len(data) != size:
            raise OSError(
                f'BlobReader short read at {self._position}: wanted {size}, got {len(data)}'
            )
        self._position += size
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


@dataclass(frozen=True, slots=True)
class ShardRef:
    """Object identity and expected size for provider acquire; no parsed MCAP data."""

    key: str
    size: int


class IndexSource(str, Enum):
    """How the shard's inventory was obtained; not its current storage location."""

    MCAP_SUMMARY = 'range_summary'
    LINEAR_SCAN = 'download_scan'


@dataclass(frozen=True, slots=True)
class ShardRotation:
    """Recording file-rotation identity and ordering, not a spatial rotation."""

    recording_id: str
    part_index: int
    video_continuation: bool

    def _identity(self) -> _RotationIdentity:
        return self.recording_id, self.part_index, self.video_continuation


@dataclass(frozen=True, slots=True)
class ShardEntry:
    """Serializable inventory for one shard, containing topics and optional rotation.

    Unlike _FileIndex, this holds no channel IDs or protobuf schema records needed
    by local decoders. Its ref property supplies only the provider-facing key/size.
    """

    key: str
    size: int
    start_ns: int | None
    end_ns: int | None
    chunk_indexed: bool
    truncated: bool
    topic_info: tuple[TopicInfo, ...]
    metadata_names: tuple[str, ...] | None
    index_source: IndexSource
    messages: int
    rotation: ShardRotation | None = None

    @property
    def ref(self) -> ShardRef:
        return ShardRef(self.key, self.size)


def _ordered(entries: Sequence[ShardEntry]) -> tuple[ShardEntry, ...]:
    return tuple(
        entries[i]
        for i in _rotation_order(
            [e.start_ns for e in entries],
            [e.rotation._identity() if e.rotation else None for e in entries],
            [0] * len(entries),
        )
    )


def _fields(data, required, optional=()):
    if not isinstance(data, dict):
        raise ValueError('catalog object must be a JSON object')
    missing, unknown = set(required) - data.keys(), data.keys() - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(
            f'invalid catalog fields: missing={sorted(missing)}, unknown={sorted(unknown)}'
        )


def _typed(value, kind, name):
    if type(value) is not kind:
        raise ValueError(f'{name} must be {kind.__name__}')
    return value


def _nonnegative(value, name):
    value = _typed(value, int, name)
    if value < 0:
        raise ValueError(f'{name} must be nonnegative')
    return value


@dataclass(frozen=True, slots=True)
class ShardCatalog:
    """Versioned ordered shard inventory, independent of local materialization."""

    version: int
    shards: tuple[ShardEntry, ...]

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError(f'unsupported shard catalog version: {self.version!r}')
        keys = [e.key for e in self.shards]
        if len(set(keys)) != len(keys):
            raise ValueError('duplicate shard keys in catalog')
        if self.shards != _ordered(self.shards):
            raise ValueError('catalog shards must be in recording/part time order')

    def to_json(self) -> dict[str, Any]:
        shards = []
        for entry in self.shards:
            row = asdict(entry)
            row['index_source'] = entry.index_source.value
            row['topic_info'] = [
                dict(topic=t.topic, schema_name=t.schema_name, messages=t.message_count)
                for t in entry.topic_info
            ]
            row['metadata_names'] = (
                list(entry.metadata_names) if entry.metadata_names is not None else None
            )
            shards.append(row)
        return dict(version=self.version, shards=shards)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ShardCatalog:
        _fields(data, ('version', 'shards'))
        _typed(data['shards'], list, 'shards')
        entries = []
        for row in data['shards']:
            _fields(
                row,
                (
                    'key',
                    'size',
                    'start_ns',
                    'end_ns',
                    'chunk_indexed',
                    'truncated',
                    'topic_info',
                    'metadata_names',
                    'index_source',
                    'messages',
                ),
                ('rotation',),
            )
            _typed(row['key'], str, 'key')
            if not row['key']:
                raise ValueError('shard key cannot be empty')
            for name in ('size', 'messages'):
                _nonnegative(row[name], name)
            for name in ('start_ns', 'end_ns'):
                if row[name] is not None:
                    _nonnegative(row[name], name)
            if (row['start_ns'] is None) != (row['end_ns'] is None):
                raise ValueError('shard time bounds must both be known or unknown')
            if row['start_ns'] is not None and row['start_ns'] > row['end_ns']:
                raise ValueError('shard start_ns exceeds end_ns')
            for name in ('chunk_indexed', 'truncated'):
                _typed(row[name], bool, name)
            source = IndexSource(row['index_source'])
            if source == IndexSource.LINEAR_SCAN and row['chunk_indexed']:
                raise ValueError('a scan cannot manufacture chunk indexes')
            topics = []
            for topic in _typed(row['topic_info'], list, 'topic_info'):
                _fields(topic, ('topic', 'schema_name', 'messages'))
                topics.append(
                    TopicInfo(
                        _typed(topic['topic'], str, 'topic'),
                        _typed(topic['schema_name'], str, 'schema_name'),
                        _nonnegative(topic['messages'], 'topic messages'),
                    )
                )
            names = row['metadata_names']
            if names is not None:
                names = tuple(
                    _typed(n, str, 'metadata name') for n in _typed(names, list, 'metadata_names')
                )
            rotation = row.get('rotation')
            if rotation is not None:
                _fields(rotation, ('recording_id', 'part_index', 'video_continuation'))
                rotation = ShardRotation(
                    _typed(rotation['recording_id'], str, 'recording_id'),
                    _nonnegative(rotation['part_index'], 'part_index'),
                    _typed(rotation['video_continuation'], bool, 'video_continuation'),
                )
                if not rotation.recording_id:
                    raise ValueError('recording_id cannot be empty')
            values = dict(row)
            values.update(
                topic_info=tuple(topics),
                metadata_names=names,
                rotation=rotation,
                index_source=source,
            )
            entries.append(ShardEntry(**values))
        return cls(data['version'], tuple(entries))


class ShardProvider(Protocol):
    """A stage-side way to pin a local shard, implemented outside schema."""

    def acquire(self, shard: ShardRef) -> AbstractContextManager[Path]: ...

    def prefetch(self, shard: ShardRef) -> None: ...


class BaseSession(Protocol):
    @property
    def metadata(self) -> SessionMeta: ...

    @property
    def calibration(self) -> Calibration: ...

    @property
    def device(self) -> str | None: ...

    def topics(self) -> list[TopicInfo]: ...

    def stream_summaries(self) -> list[StreamSummary]: ...


_log = logging.getLogger(__name__)


class ShardCatalogBuilder:
    def __init__(self) -> None:
        self._entries: list[ShardEntry] = []
        self._built = False

    def _require_open(self) -> None:
        if self._built:
            raise ValueError('ShardCatalogBuilder: cannot index shards after build()')

    def index_remote_shard(
        self,
        shard: ShardRef,
        *,
        reader: BlobReader,
        provider: ShardProvider,
    ) -> None:
        """Index remote ranges, acquiring one local shard only if its summary is unavailable."""
        self._require_open()
        if reader.size != shard.size:
            raise ValueError(f'{shard.key}: remote reader size differs from shard size')
        try:
            entry = self._entry_from_reader(shard.key, reader)
        except SummaryUnavailableError as exc:
            _log.warning(
                '%s: remote summary unavailable; acquiring shard for download_scan', shard.key
            )
            with provider.acquire(shard) as path:
                if path.stat().st_size != shard.size:
                    raise ValueError(
                        f'{shard.key}: acquired file size differs from shard size'
                    ) from exc
                entry = self._entry_from_path(shard.key, path)
        self._entries.append(entry)

    def index_local_shard(self, key: str, path: Path) -> None:
        """Index a local MCAP, scanning its recoverable prefix when necessary."""
        self._require_open()
        self._entries.append(self._entry_from_path(key, path))

    @staticmethod
    def _entry_from_reader(key: str, reader: BlobReader) -> ShardEntry:
        with io.BufferedReader(BlobReaderIO(reader)) as file:
            idx = _FileIndex(file, label=key, require_index=True)
            rows = []
            if ROTATION_METADATA in (idx.metadata_names or ()):
                file.seek(0)
                indexed_reader = make_reader(file)
                if indexed_reader.get_summary() is None:
                    raise SummaryUnavailableError(f'{key}: no MCAP summary for metadata lookup')
                rows = [
                    m.metadata
                    for m in indexed_reader.iter_metadata()
                    if m.name == ROTATION_METADATA
                ]
            return ShardCatalogBuilder._entry(
                key,
                reader.size,
                idx,
                rows,
                IndexSource.MCAP_SUMMARY,
            )

    @staticmethod
    def _entry_from_path(key: str, path: Path) -> ShardEntry:
        path = Path(path)
        idx = _FileIndex(path)
        rows = []
        if idx.metadata_names is None or ROTATION_METADATA in idx.metadata_names:
            if idx.truncated:
                rows = [m.metadata for m in _linear_metadata(path) if m.name == ROTATION_METADATA]
            else:
                with path.open('rb') as file:
                    rows = [
                        m.metadata
                        for m in make_reader(file).iter_metadata()
                        if m.name == ROTATION_METADATA
                    ]
        source = IndexSource.LINEAR_SCAN if idx.metadata_names is None else IndexSource.MCAP_SUMMARY
        return ShardCatalogBuilder._entry(key, path.stat().st_size, idx, rows, source)

    @staticmethod
    def _entry(
        key: str,
        size: int,
        idx: _FileIndex,
        rows: Sequence[Mapping[str, str]],
        source: IndexSource,
    ) -> ShardEntry:
        topics = tuple(TopicInfo(t, s, n) for t, s, n in idx.topics())
        rotation = _rotation_from_metadata(rows, key)
        return ShardEntry(
            key,
            size,
            idx.start_ns,
            idx.end_ns,
            idx.chunk_indexed,
            idx.truncated,
            topics,
            idx.metadata_names,
            source,
            sum(idx.counts.values()),
            ShardRotation(*rotation) if rotation else None,
        )

    def build(self) -> ShardCatalog:
        catalog = ShardCatalog(1, _ordered(self._entries))
        self._built = True
        return catalog


class OnDemandSession:
    """Sequential decoded sampling and indexed windows over provider-pinned shards.

    Catalog inventory never pins local paths. Metadata/calibration load one healthy
    part lazily; pixel sampling pins one part at a time; sparse queries pin only
    the parts intersecting their half-open window. Closing generators releases pins.
    """

    def __init__(
        self,
        catalog: ShardCatalog,
        provider: ShardProvider,
        *,
        device: str | None = None,
        reorder_ns: int = 50_000_000,
        topic_mode: str = 'auto',
        sidecars: Sequence[Path | str] = (),
        require_index: bool = False,
    ):
        if sidecars:
            raise UnsupportedOperationError('OnDemandSession does not support sidecars')
        if not catalog.shards:
            raise ValueError('OnDemandSession: no MCAP shards in catalog')
        if topic_mode not in ('auto', 'preserve'):
            raise ValueError("topic_mode must be 'auto' or 'preserve'")
        if topic_mode == 'preserve' and device is not None:
            raise ValueError("topic_mode='preserve' cannot filter a device")
        if require_index:
            for entry in catalog.shards:
                if entry.index_source != IndexSource.MCAP_SUMMARY or entry.truncated:
                    raise SummaryUnavailableError(
                        f'{entry.key}: no MCAP summary for strict session'
                    )
        self.catalog, self._provider = catalog, provider
        self._entries = {entry.key: entry for entry in catalog.shards}
        devices = {
            d
            for e in catalog.shards
            if (d := _detect_device(t.topic for t in e.topic_info)) is not None
        }
        if topic_mode == 'preserve':
            self._device = None
        elif device is not None:
            self._device = device
        else:
            self._device = next(iter(devices)) if len(devices) == 1 else None
        self._reorder_ns = reorder_ns
        self._require_index = require_index
        self._initial_metadata: SessionMeta | None = None
        self._calibration: Calibration | None = None
        self._cadence: dict[str, KeyframeCadence | None] = {}
        self._sparse_stats: dict[str, int] = {}

    @classmethod
    def open(cls, catalog_path: Path | str, provider: ShardProvider, **kwargs) -> OnDemandSession:
        catalog = ShardCatalog.from_json(json.loads(Path(catalog_path).read_text()))
        return cls(catalog, provider, **kwargs)

    def _local(self, paths, *, require_index=None):
        return Session(
            paths,
            device=self.device,
            reorder_ns=self._reorder_ns,
            topic_mode='preserve' if self.device is None else 'auto',
            require_index=self._require_index if require_index is None else require_index,
        )

    @property
    def device(self) -> str | None:
        return self._device

    def topics(self) -> list[TopicInfo]:
        agg = {}
        for entry in self.catalog.shards:
            for row in entry.topic_info:
                topic = strip_device_topic_prefix(row.topic, self.device)
                if topic is not None:
                    schema, count = agg.get(topic, (row.schema_name, 0))
                    agg[topic] = (row.schema_name or schema, count + row.message_count)
        return [TopicInfo(t, s, n) for t, (s, n) in sorted(agg.items())]

    def stream_summaries(self) -> list[StreamSummary]:
        return [
            StreamSummary(
                0,
                tuple(
                    FileSummary(
                        path=e.key,
                        size=e.size,
                        status='truncated' if e.truncated else 'ok',
                        messages=e.messages,
                        start_ns=e.start_ns,
                        end_ns=e.end_ns,
                        metadata_names=e.metadata_names,
                    )
                    for e in self.catalog.shards
                ),
            )
        ]

    def _load_initial(self):
        if self._initial_metadata is not None:
            return
        entry = next((e for e in self.catalog.shards if not e.truncated), self.catalog.shards[0])
        with self._provider.acquire(entry.ref) as path:
            session = self._local([path])
            metadata, calibration = session.metadata, session.calibration
        self._initial_metadata, self._calibration = metadata, calibration

    @property
    def metadata(self) -> SessionMeta:
        self._load_initial()
        starts = [e.start_ns for e in self.catalog.shards if e.start_ns is not None]
        ends = [e.end_ns for e in self.catalog.shards if e.end_ns is not None]
        return replace(
            self._initial_metadata,
            device=self.device,
            start_ns=min(starts) if starts else None,
            end_ns=max(ends) if ends else None,
        )

    @property
    def calibration(self) -> Calibration:
        self._load_initial()
        return self._calibration

    def _require_video_topic(self, topic):
        info = next((t for t in self.topics() if t.topic == topic), None)
        if info is None or info.schema_name != VIDEO_SCHEMA:
            raise ValueError(f'{topic!r} is not a video topic in this session')

    def _matching(self, topic=None, start_ns=None, end_ns=None):
        for entry in self.catalog.shards:
            if start_ns is not None and entry.end_ns is not None and entry.end_ns < start_ns:
                continue
            if end_ns is not None and entry.start_ns is not None and entry.start_ns >= end_ns:
                continue
            if topic is not None and not any(
                t.schema_name == VIDEO_SCHEMA
                and strip_device_topic_prefix(t.topic, self.device) == topic
                for t in entry.topic_info
            ):
                continue
            yield entry

    def keyframe_cadence(self, topic: str) -> KeyframeCadence | None:
        self._require_video_topic(topic)
        if topic not in self._cadence:
            cadence = None
            for entry in self._matching(topic):
                with self._provider.acquire(entry.ref) as path:
                    cadence = self._local([path]).keyframe_cadence(topic)
                if cadence is not None:
                    break
            self._cadence[topic] = cadence
        return self._cadence[topic]

    def keyframe_stream(
        self,
        topic: str,
        *,
        every_n: int = 1,
        start_ns: int | None = None,
        end_ns: int | None = None,
        gray: bool = False,
        gpu: bool = False,
    ) -> Iterator[Frame]:
        if every_n < 1:
            raise ValueError(f'keyframe_stream: every_n must be >= 1, got {every_n}')
        self._require_video_topic(topic)
        return self._keyframe_frames(topic, every_n, start_ns, end_ns, gray, gpu)

    def _keyframe_frames(self, topic, every_n, start_ns, end_ns, gray, gpu):
        progress = _KeyframeProgress()
        for entry in self._matching(topic, start_ns, end_ns):
            with self._provider.acquire(entry.ref) as path:
                session = self._local([path])
                yield from session._keyframe_frames(
                    topic,
                    every_n=every_n,
                    start_ns=start_ns,
                    end_ns=end_ns,
                    make_decoders=session._decoder_binder(gray, gpu, 'keyframe_stream'),
                    progress=progress,
                )
        progress.require_keyframes(topic)

    def require_chunk_indexes(self) -> None:
        for entry in self.catalog.shards:
            if not entry.chunk_indexed:
                raise ValueError(f'{entry.key}: sparse reads require MCAP chunk indexes')

    def keyframe_records(
        self,
        topics: Sequence[str],
        *,
        start_ns: int,
        end_ns: int,
    ) -> Iterator[Record]:
        if isinstance(topics, str) or not topics or len(set(topics)) != len(topics):
            raise ValueError('keyframe_records: need distinct video topics')
        if start_ns >= end_ns:
            raise ValueError('keyframe_records: require start_ns < end_ns')
        for topic in topics:
            self._require_video_topic(topic)
        entries = [
            e
            for e in self._matching(start_ns=start_ns, end_ns=end_ns)
            if any(
                t.schema_name == VIDEO_SCHEMA
                and strip_device_topic_prefix(t.topic, self.device) in topics
                for t in e.topic_info
            )
        ]
        for entry in entries:
            if not entry.chunk_indexed:
                raise ValueError(f'{entry.key}: sparse reads require MCAP chunk indexes')
        return self._keyframe_records(entries, topics, start_ns, end_ns)

    def _keyframe_records(self, entries, topics, start_ns, end_ns):
        if not entries:
            return
        with ExitStack() as stack:
            paths = [stack.enter_context(self._provider.acquire(e.ref)) for e in entries]
            session = self._local(paths, require_index=True)
            rows = session.keyframe_records(topics, start_ns=start_ns, end_ns=end_ns)
            try:
                yield from rows
            finally:
                rows.close()
                for key, value in session.sparse_read_stats.items():
                    self._sparse_stats[key] = self._sparse_stats.get(key, 0) + value

    @property
    def sparse_read_stats(self) -> dict[str, int]:
        return dict(self._sparse_stats)

    @contextmanager
    def part_session(self, part: FileSummary):
        entry = self._entries[part.path]
        with self._provider.acquire(entry.ref) as path:
            yield self._local([path], require_index=True)

    def stream(self, *args, **kwargs):
        raise UnsupportedOperationError('OnDemandSession does not support full stream() reads')
