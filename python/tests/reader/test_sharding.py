"""On-demand reads preserve eager semantics without retaining unpinned files."""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from dataclasses import replace

import numpy as np
import pytest
from _helpers import FRAME_DT, T0, indexed_frames, unindexed_mcap
from mcap.exceptions import InvalidMagic
from mcap.writer import IndexType, Writer
from test_rotation_continuation import write_rows
from test_truncated import _chop_tail, _summary_start

from visio_schema import make_channel
from visio_schema.reader import (
    IndexSource,
    OnDemandSession,
    Session,
    ShardCatalog,
    ShardCatalogBuilder,
    ShardRef,
    SummaryUnavailableError,
    TopicInfo,
    UnsupportedOperationError,
)
from visio_schema.reader.session import _FileIndex
from visio_schema.reader.sharding import _ShardReaderIO

CAM = '/ego/camera/0'


class LocalProvider:
    def __init__(self, paths=(), *, remote=None):
        self.paths = {p.name: p for p in paths}
        self.remote = remote or {}
        self.pinned, self.events, self.ranges = set(), [], []
        self.peak = self.list_calls = 0

    def list_shards(self):
        self.list_calls += 1
        refs = {key: ShardRef(key, path.stat().st_size) for key, path in self.paths.items()}
        refs.update({key: ShardRef(key, len(data)) for key, data in self.remote.items()})
        return list(refs.values())

    def read_at(self, shard, offset, size):
        self.ranges.append((shard.key, offset, size))
        if shard.key in self.remote:
            return self.remote[shard.key][offset : offset + size]
        with self.paths[shard.key].open('rb') as file:
            file.seek(offset)
            return file.read(size)

    @contextmanager
    def acquire(self, shard):
        assert shard.size == self.paths[shard.key].stat().st_size
        assert shard.key not in self.pinned
        self.pinned.add(shard.key)
        self.peak = max(self.peak, len(self.pinned))
        self.events.append(('acquire', shard.key))
        try:
            yield self.paths[shard.key]
        finally:
            self.pinned.remove(shard.key)
            self.events.append(('release', shard.key))

    def prefetch(self, shard):
        self.events.append(('prefetch', shard.key))


@pytest.fixture
def builder():
    return ShardCatalogBuilder()


@pytest.fixture
def shards(rec):
    paths = []
    for i in range(3):
        b = rec(f'{2 - i}.mcap', device='GILABS-test', capture={'id': 'episode'})
        b.add_camera(CAM, indexed_frames(15), t0=T0 + i * 15 * FRAME_DT, keyint=5)
        b.add_camera(
            '/ego/camera/1', indexed_frames(15), t0=T0 + i * 15 * FRAME_DT + 1000, keyint=5
        )
        b.add_frame_info(CAM, n=15, t0=T0 + i * 15 * FRAME_DT)
        paths.append(b.write(chunk_size=400))
    return paths


def sessions(builder, shards, **kwargs):
    provider = LocalProvider(shards)
    for path in reversed(shards):
        builder.index_remote_shard(ShardRef(path.name, path.stat().st_size), provider=provider)
    catalog = builder.build()
    return Session(shards, **kwargs), OnDemandSession(catalog, provider, **kwargs), provider


def test_from_provider_accepts_empty_inventory():
    provider = LocalProvider()
    catalog = ShardCatalogBuilder.from_provider(provider)
    assert catalog == ShardCatalog(version=1, shards=())
    assert provider.list_calls == 1
    assert provider.ranges == provider.events == []
    with pytest.raises(ValueError, match='no MCAP shards'):
        OnDemandSession(catalog, provider)


def test_from_provider_indexes_and_orders_all_shards(builder, shards):
    provider = LocalProvider(reversed(shards))
    catalog = ShardCatalogBuilder.from_provider(provider)
    for path in shards:
        builder.index_local_shard(path.name, path)
    assert catalog == builder.build()
    assert [entry.key for entry in catalog.shards] == [path.name for path in shards]
    assert {key for key, _offset, _size in provider.ranges} == {path.name for path in shards}
    assert provider.events == []
    session = OnDemandSession(catalog, provider)
    assert session.topics() == Session(shards).topics()
    assert session.metadata == Session(shards).metadata
    assert provider.list_calls == 1
    assert not provider.pinned


def test_from_provider_reuses_single_shard_fallback(rec, tmp_path, monkeypatch):
    healthy = rec('healthy.mcap').add_camera(CAM, indexed_frames(3)).write()
    unindexed = unindexed_mcap(tmp_path / 'unindexed.mcap')
    provider = LocalProvider([healthy, unindexed])
    calls = []
    index_remote_shard = ShardCatalogBuilder.index_remote_shard

    def record_index(self, shard, *, provider):
        calls.append(shard)
        return index_remote_shard(self, shard, provider=provider)

    monkeypatch.setattr(ShardCatalogBuilder, 'index_remote_shard', record_index)
    catalog = ShardCatalogBuilder.from_provider(provider)
    assert [ref.key for ref in calls] == [healthy.name, unindexed.name]
    entries = {entry.key: entry for entry in catalog.shards}
    assert entries[healthy.name].index_source == IndexSource.MCAP_SUMMARY
    assert entries[unindexed.name].index_source == IndexSource.LINEAR_SCAN
    assert not entries[unindexed.name].chunk_indexed
    assert provider.list_calls == 1
    assert provider.events == [('acquire', unindexed.name), ('release', unindexed.name)]
    assert not provider.pinned


def test_from_provider_propagates_listing_failure():
    class FailingProvider(LocalProvider):
        def list_shards(self):
            raise OSError('listing failed')

    provider = FailingProvider()
    with pytest.raises(OSError, match='listing failed'):
        ShardCatalogBuilder.from_provider(provider)
    assert provider.ranges == provider.events == []


def test_from_provider_handles_5000_shards(builder, rec):
    path = rec().add_camera(CAM, indexed_frames(1)).write()
    builder.index_local_shard(path.name, path)
    (template,) = builder.build().shards
    refs = tuple(ShardRef(f'{i:04}.mcap', template.size) for i in range(5000))
    calls = []

    class ListingProvider(LocalProvider):
        def list_shards(self):
            self.list_calls += 1
            return refs

    class InventoryBuilder(ShardCatalogBuilder):
        def index_remote_shard(self, shard, *, provider):
            calls.append(shard)
            self._entries.append(replace(template, key=shard.key, size=shard.size))

    provider = ListingProvider()
    catalog = InventoryBuilder.from_provider(provider)
    assert len(catalog.shards) == 5000
    assert tuple(calls) == refs
    assert tuple(entry.ref for entry in catalog.shards) == refs
    assert provider.list_calls == 1
    assert provider.ranges == provider.events == []


def test_catalog_roundtrip_and_inventory(builder, shards, tmp_path):
    eager, lazy, provider = sessions(builder, shards)
    catalog = ShardCatalog.from_json(json.loads(json.dumps(lazy.catalog.to_json())))
    assert catalog == lazy.catalog
    assert set(catalog.to_json()) == {'version', 'shards'}
    assert isinstance(catalog.shards[0].index_source, IndexSource)
    assert isinstance(catalog.shards[0].topic_info[0], TopicInfo)
    topic_row = catalog.to_json()['shards'][0]['topic_info'][0]
    assert topic_row['messages'] == catalog.shards[0].topic_info[0].message_count
    assert [s.key for s in catalog.shards] == [p.name for p in shards]
    assert catalog.shards[0].ref == ShardRef(shards[0].name, shards[0].stat().st_size)
    assert lazy.topics() == eager.topics()
    actual, expected = lazy.stream_summaries()[0], eager.stream_summaries()[0]
    assert actual.origin == expected.origin == 0
    assert [
        replace(f, path=e.path) for f, e in zip(actual.files, expected.files, strict=True)
    ] == list(expected.files)
    assert provider.events == []
    assert lazy.metadata == eager.metadata
    assert provider.peak == 1 and not provider.pinned
    assert lazy.calibration.cams == eager.calibration.cams == {}
    assert len(provider.events) == 2
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps(catalog.to_json()))
    assert OnDemandSession.open(path, provider=provider, sidecars=[]).topics() == eager.topics()


@pytest.mark.parametrize('where', ['catalog', 'shard', 'topic', 'rotation'])
def test_catalog_rejects_storage_fields(builder, shards, where):
    builder.index_local_shard(shards[0].name, shards[0])
    data = builder.build().to_json()
    row = data if where == 'catalog' else data['shards'][0]
    if where == 'topic':
        row = row['topic_info'][0]
    if where == 'rotation':
        row['rotation'] = {'recording_id': 'r', 'part_index': 0, 'video_continuation': False}
        row = row['rotation']
    row['etag'] = 'not-public'
    with pytest.raises(ValueError, match='unknown'):
        ShardCatalog.from_json(data)


@pytest.mark.parametrize('version', [0, 2, True, '1'])
def test_catalog_version_checked(builder, version):
    data = builder.build().to_json()
    data['version'] = version
    with pytest.raises(ValueError, match='version'):
        ShardCatalog.from_json(data)


def test_builder_registers_shards_and_freezes_after_build(builder, shards):
    assert builder.index_local_shard(shards[0].name, shards[0]) is None
    provider = LocalProvider([shards[1]])
    assert (
        builder.index_remote_shard(
            ShardRef(shards[1].name, shards[1].stat().st_size),
            provider=provider,
        )
        is None
    )
    catalog = builder.build()
    assert [entry.key for entry in catalog.shards] == [p.name for p in shards[:2]]
    assert builder.build() == catalog
    with pytest.raises(ValueError, match='after build'):
        builder.index_local_shard(shards[2].name, shards[2])
    with pytest.raises(ValueError, match='after build'):
        builder.index_remote_shard(
            ShardRef(shards[2].name, shards[2].stat().st_size),
            provider=provider,
        )
    assert builder.build() == catalog
    assert provider.events == []


@pytest.mark.parametrize('size', [-1, True, 1.5])
def test_invalid_remote_shard_size_is_fatal(builder, size):
    provider = LocalProvider()
    with pytest.raises(ValueError, match='ShardRef.size'):
        builder.index_remote_shard(ShardRef('part.mcap', size), provider=provider)
    assert provider.ranges == provider.events == []
    assert builder.build().shards == ()


def test_fallback_size_mismatch_releases_handle(builder, tmp_path):
    path = unindexed_mcap(tmp_path / 'part.mcap')
    data = path.read_bytes()

    class WrongSizeProvider(LocalProvider):
        @contextmanager
        def acquire(self, shard):
            self.pinned = True
            try:
                yield tmp_path / 'wrong.mcap'
            finally:
                self.pinned = False

    (tmp_path / 'wrong.mcap').write_bytes(b'wrong')
    provider = WrongSizeProvider(remote={path.name: data})
    with pytest.raises(ValueError, match='acquired file size'):
        builder.index_remote_shard(
            ShardRef(path.name, len(data)),
            provider=provider,
        )
    assert not provider.pinned
    assert builder.build().shards == ()


def test_fallback_parse_failure_releases_handle(builder, tmp_path):
    path = unindexed_mcap(tmp_path / 'part.mcap')
    data = path.read_bytes()
    path.write_bytes(b'x' * len(data))
    provider = LocalProvider([path], remote={path.name: data})
    with pytest.raises(InvalidMagic):
        builder.index_remote_shard(
            ShardRef(path.name, len(data)),
            provider=provider,
        )
    assert not provider.pinned
    assert provider.events == [('acquire', path.name), ('release', path.name)]
    assert builder.build().shards == ()


def test_shard_adapter_seek_and_close():
    shard = ShardRef('part.mcap', 8)
    provider = LocalProvider(remote={shard.key: b'abcdefgh'})
    with _ShardReaderIO(provider, shard) as file:
        assert file.read(2) == b'ab'
        assert file.seek(-1, io.SEEK_CUR) == 1
        buf = bytearray(2)
        assert file.readinto(buf) == 2 and buf == b'bc'
        assert file.seek(-2, io.SEEK_END) == 6
        assert file.read() == b'gh'
        file.seek(20)
        assert file.read() == b''
    with pytest.raises(ValueError, match='closed'):
        file.tell()


def test_shared_provider_keeps_shard_cursors_independent():
    provider = LocalProvider(remote={'first': b'abcdefgh', 'second': b'12345678'})
    first_ref, second_ref = ShardRef('first', 8), ShardRef('second', 8)
    with (
        _ShardReaderIO(provider, first_ref) as first,
        _ShardReaderIO(provider, second_ref) as second,
    ):
        assert first.read(2) == b'ab'
        assert second.read(3) == b'123'
        assert first.tell() == 2 and second.tell() == 3
        assert first.read(2) == b'cd'
        assert second.read(2) == b'45'
        first.seek(8)
        calls = len(provider.ranges)
        assert first.read(1) == b'' and len(provider.ranges) == calls
    assert provider.ranges == [('first', 0, 2), ('second', 0, 3), ('first', 2, 2), ('second', 3, 2)]
    assert provider.events == []


def test_reader_adapters_are_not_public_api():
    from visio_schema import reader
    from visio_schema.reader import sharding

    for name in ('BlobReader', 'BlobReaderIO'):
        assert not hasattr(reader, name)
        assert not hasattr(sharding, name)
        assert name not in reader.__all__
    assert '_ShardReaderIO' not in reader.__all__


def test_range_summary_uses_bounded_reads(builder, rec):
    path = rec().add_camera(CAM, indexed_frames(20)).write(chunk_size=200)
    data = path.read_bytes()
    provider = LocalProvider(remote={'remote.mcap': data})
    builder.index_remote_shard(ShardRef('remote.mcap', len(data)), provider=provider)
    (entry,) = builder.build().shards
    assert provider.events == []
    assert entry.index_source == 'range_summary'
    assert entry.messages == 20 and entry.chunk_indexed and not entry.truncated
    assert entry.start_ns == T0 and entry.end_ns == T0 + 19 * FRAME_DT
    assert entry.topic_info[0].schema_name == 'foxglove.CompressedVideo'
    cut = _summary_start(path)
    assert all(offset < 8 or offset >= cut for _key, offset, _size in provider.ranges)
    assert sum(size for _key, _offset, size in provider.ranges) < len(data) // 2
    assert len(provider.ranges) < 20
    assert {key for key, _offset, _size in provider.ranges} == {'remote.mcap'}


def test_filelike_index_does_not_close_caller_stream(builder, shards):
    file = io.BytesIO(shards[0].read_bytes())
    idx = _FileIndex(file, label='remote-part', require_index=True)
    assert not file.closed and idx.path == 'remote-part'
    assert idx.topics() == _FileIndex(shards[0]).topics()
    with io.FileIO(shards[0], 'r') as raw:
        assert _FileIndex(raw, label='raw-part').topics() == idx.topics()
        assert not raw.closed
    with pytest.raises(SummaryUnavailableError, match='diagnostic-label'):
        _FileIndex(io.BytesIO(shards[0].read_bytes()[:-30]), label='diagnostic-label')


def test_missing_summary_acquires_local_fallback(builder, tmp_path, caplog):
    path = unindexed_mcap(tmp_path / 'no-summary.mcap')
    provider = LocalProvider([path])
    builder.index_remote_shard(
        ShardRef(path.name, path.stat().st_size),
        provider=provider,
    )
    (entry,) = builder.build().shards
    assert entry.index_source == 'download_scan'
    assert not entry.chunk_indexed and not entry.truncated
    assert entry.metadata_names is None and entry.messages > 0
    assert provider.events == [('acquire', path.name), ('release', path.name)]
    assert not provider.pinned
    assert path.name in caplog.text and 'download_scan' in caplog.text


def test_unreadable_summary_and_truncated_prefix(builder, rec):
    path = rec().add_camera(CAM, indexed_frames(10)).write(chunk_size=200)
    _chop_tail(path)
    provider = LocalProvider([path])
    builder.index_remote_shard(
        ShardRef(path.name, path.stat().st_size),
        provider=provider,
    )
    catalog = builder.build()
    (entry,) = catalog.shards
    assert entry.truncated and not entry.chunk_indexed
    assert entry.index_source == 'download_scan' and entry.messages == 10
    assert not provider.pinned
    lazy = OnDemandSession(catalog, provider)
    assert len(list(lazy.keyframe_stream(CAM))) == 10
    with pytest.raises(ValueError, match='chunk indexes'):
        lazy.require_chunk_indexes()
    with pytest.raises(SummaryUnavailableError):
        OnDemandSession(lazy.catalog, provider, require_index=True)
    assert not provider.pinned


def test_invalid_magic_and_network_error_do_not_trigger_fallback(builder):
    provider = LocalProvider(remote={'not.mcap': b'not mcap' * 10})
    with pytest.raises(InvalidMagic):
        builder.index_remote_shard(ShardRef('not.mcap', 80), provider=provider)
    assert provider.events == []

    class FailingProvider(LocalProvider):
        def read_at(self, shard, offset, size):
            if offset > 8:
                raise PermissionError('denied')
            return super().read_at(shard, offset, size)

    failing = FailingProvider(remote={'network.mcap': b'\x89MCAP0\r\n' + b'x' * 100})
    with pytest.raises(PermissionError, match='denied'):
        builder.index_remote_shard(ShardRef('network.mcap', 108), provider=failing)
    assert failing.events == []
    assert builder.build().shards == ()


def test_corrupt_summary_acquires_local_fallback(builder, rec):
    path = rec().add_camera(CAM, indexed_frames(4)).write()
    data = bytearray(path.read_bytes())
    start = _summary_start(path)
    data[start + 1 : start + 9] = (2**63).to_bytes(8, 'little')
    path.write_bytes(data)
    provider = LocalProvider([path])
    builder.index_remote_shard(
        ShardRef(path.name, path.stat().st_size),
        provider=provider,
    )
    (entry,) = builder.build().shards
    assert not provider.pinned
    assert provider.events == [('acquire', path.name), ('release', path.name)]
    assert entry.index_source == 'download_scan'
    assert entry.truncated and not entry.chunk_indexed and entry.messages == 4


def test_short_remote_read_is_an_io_error_not_summary_fallback(builder, shards):
    class ShortProvider(LocalProvider):
        def read_at(self, shard, offset, size):
            return super().read_at(shard, offset, size)[:-1]

    provider = ShortProvider(remote={'short.mcap': shards[0].read_bytes()})
    with pytest.raises(OSError, match='short read'):
        builder.index_remote_shard(
            ShardRef('short.mcap', shards[0].stat().st_size),
            provider=provider,
        )
    assert provider.events == []
    assert builder.build().shards == ()


def test_no_chunk_index_is_not_fabricated(builder, rec):
    b = rec().add_camera(CAM, indexed_frames(2))
    channel = make_channel(CAM, 'foxglove.CompressedVideo', stream_id=0)
    with b.path.open('wb') as file:
        writer = Writer(file, index_types=IndexType.MESSAGE)
        writer.start()
        sid = writer.register_schema(
            name=channel.schema_name, encoding='protobuf', data=channel.schema
        )
        cid = writer.register_channel(topic=CAM, message_encoding='protobuf', schema_id=sid)
        for _, _, t, data in b._rows:
            writer.add_message(channel_id=cid, log_time=t, publish_time=t, data=data)
        writer.finish()
    builder.index_remote_shard(
        ShardRef(b.path.name, b.path.stat().st_size),
        provider=LocalProvider([b.path]),
    )
    catalog = builder.build()
    (entry,) = catalog.shards
    assert entry.index_source == 'range_summary' and not entry.chunk_indexed
    lazy = OnDemandSession(catalog, LocalProvider([b.path]), require_index=True)
    with pytest.raises(ValueError, match='chunk indexes'):
        lazy.keyframe_records([CAM], start_ns=T0, end_ns=T0 + FRAME_DT)


@pytest.mark.parametrize('every_n', [1, 2, 4])
@pytest.mark.parametrize('window', [False, True])
def test_three_shard_decoded_parity_and_phase(builder, shards, every_n, window):
    eager, lazy, provider = sessions(builder, shards)
    kwargs = {'every_n': every_n, 'gray': True}
    if window:
        kwargs.update(start_ns=T0 + 6 * FRAME_DT, end_ns=T0 + 36 * FRAME_DT)
    expected, actual = (
        list(eager.keyframe_stream(CAM, **kwargs)),
        list(lazy.keyframe_stream(CAM, **kwargs)),
    )
    assert [f.t_ns for f in actual] == [f.t_ns for f in expected]
    for a, b in zip(actual, expected, strict=True):
        assert a.topic == b.topic and a.frame_id == b.frame_id and a.exposure == b.exposure
        np.testing.assert_array_equal(a.image, b.image)
    assert provider.peak == 1 and not provider.pinned
    assert lazy.keyframe_cadence(CAM) == eager.keyframe_cadence(CAM)


def test_keyframe_stream_close_releases_current_part(builder, shards):
    _, lazy, provider = sessions(builder, shards)
    frames = lazy.keyframe_stream(CAM)
    next(frames)
    assert provider.pinned == {shards[0].name}
    frames.close()
    assert not provider.pinned
    assert provider.events == [('acquire', shards[0].name), ('release', shards[0].name)]


def test_sparse_cross_shard_parity_and_release(builder, shards):
    eager, lazy, provider = sessions(builder, shards, require_index=True)
    kwargs = {'start_ns': T0 + 10 * FRAME_DT, 'end_ns': T0 + 22 * FRAME_DT}
    topics = [CAM, '/ego/camera/1']
    actual, expected = (
        list(lazy.keyframe_records(topics, **kwargs)),
        list(eager.keyframe_records(topics, **kwargs)),
    )
    assert [(r.topic, r.t_ns, r.data) for r in actual] == [
        (r.topic, r.t_ns, r.data) for r in expected
    ]
    assert provider.peak == 2 and not provider.pinned
    assert lazy.sparse_read_stats == eager.sparse_read_stats
    rows = lazy.keyframe_records(topics, **kwargs)
    next(rows)
    assert provider.pinned == {p.name for p in shards[:2]}
    rows.close()
    assert not provider.pinned
    assert lazy.sparse_read_stats['files_read'] == 4


def test_acquire_failure_releases_previous_handles(builder, shards):
    _, lazy, provider = sessions(builder, shards)
    del provider.paths[shards[1].name]
    with pytest.raises(KeyError):
        list(lazy.keyframe_records([CAM], start_ns=T0, end_ns=T0 + 45 * FRAME_DT))
    assert not provider.pinned


def test_part_session_is_context_managed_on_both_sessions(builder, shards):
    eager, lazy, provider = sessions(builder, shards)
    part = lazy.stream_summaries()[0].files[1]
    with lazy.part_session(part) as other:
        assert provider.pinned == {shards[1].name}
        other.require_chunk_indexes()
        assert other.topics() == Session(shards[1]).topics()
    assert not provider.pinned
    with eager.part_session(eager.stream_summaries()[0].files[1]) as other:
        assert other.metadata == Session(shards[1]).metadata


def test_rotation_metadata_range_reads_are_bounded(builder, rec):
    recording = rec().add_camera(CAM, indexed_frames(20), keyint=5)
    path = write_rows(recording.path, [(*r, i) for i, r in enumerate(recording._rows)], 0)
    provider = LocalProvider([path])
    builder.index_remote_shard(ShardRef(path.name, path.stat().st_size), provider=provider)
    (entry,) = builder.build().shards
    assert entry.rotation.recording_id == 'test-recording'
    assert entry.rotation.part_index == 0
    assert provider.events == []
    assert all(size <= io.DEFAULT_BUFFER_SIZE for _key, _offset, size in provider.ranges)
    assert len(provider.ranges) < 20


def test_remote_metadata_without_summary_never_scans(builder, rec, monkeypatch):
    from visio_schema.reader import sharding

    recording = rec().add_camera(CAM, indexed_frames(3))
    path = write_rows(recording.path, [(*r, i) for i, r in enumerate(recording._rows)], 0)
    provider = LocalProvider([path])
    make_reader = sharding.make_reader
    calls = []

    class SummaryDisappeared:
        def get_summary(self):
            return None

        def iter_metadata(self):
            pytest.fail('remote metadata lookup attempted a summary-less linear scan')

    def metadata_reader(file):
        calls.append(file)
        return SummaryDisappeared() if len(calls) == 1 else make_reader(file)

    monkeypatch.setattr(sharding, 'make_reader', metadata_reader)
    builder.index_remote_shard(
        ShardRef(path.name, path.stat().st_size),
        provider=provider,
    )
    assert builder.build().shards[0].rotation.part_index == 0
    assert provider.events == [('acquire', path.name), ('release', path.name)]
    assert not provider.pinned


@pytest.mark.parametrize('conflict', [False, True])
def test_sparse_overlap_and_out_of_order_seam_parity(builder, rec, conflict):
    first = rec('first.mcap').add_camera(CAM, indexed_frames(20))
    second = rec('second.mcap').add_camera(CAM, indexed_frames(5), t0=T0 + 20 * FRAME_DT)
    second._rows = list(first._rows[-5:]) + second._rows
    if conflict:
        topic, schema, stamp, payload = second._rows[0]
        second._rows[0] = topic, schema, stamp, payload + b'\x98\x06\x01'
    second._rows[0], second._rows[1] = second._rows[1], second._rows[0]
    eager, lazy, provider = sessions(builder, [first.write(), second.write(sort=False)])
    kwargs = dict(start_ns=T0 + 14 * FRAME_DT, end_ns=T0 + 23 * FRAME_DT)
    if conflict:
        for session in (eager, lazy):
            with pytest.raises(ValueError, match='conflicting keyframes'):
                list(session.keyframe_records([CAM], **kwargs))
    else:
        expected = list(eager.keyframe_records([CAM], **kwargs))
        actual = list(lazy.keyframe_records([CAM], **kwargs))
        assert [(r.t_ns, r.data) for r in actual] == [(r.t_ns, r.data) for r in expected]
        assert [r.t_ns for r in actual] == [T0 + i * FRAME_DT for i in range(14, 23)]
    assert not provider.pinned


def test_rotation_order_matches_eager_despite_backlog(builder, rec):
    a = rec('first.mcap').add_camera(CAM, indexed_frames(15), t0=T0 + 100 * FRAME_DT, keyint=5)
    b = rec('second.mcap').add_camera(CAM, indexed_frames(15), t0=T0, keyint=5)
    paths = [
        write_rows(part.path, [(*r, i) for i, r in enumerate(part._rows)], n)
        for n, part in enumerate((a, b))
    ]
    eager, lazy, provider = sessions(builder, paths)
    assert [e.key for e in lazy.catalog.shards] == [p.name for p in paths]
    assert [f.t_ns for f in lazy.keyframe_stream(CAM, every_n=2)] == [
        f.t_ns for f in eager.keyframe_stream(CAM, every_n=2)
    ]
    assert lazy.catalog.shards[1].rotation.part_index == 1
    assert not provider.pinned


@pytest.mark.parametrize('empty_part_first', [True, False])
def test_keyframe_free_part_does_not_fail_a_healthy_session(builder, rec, empty_part_first):
    empty_start = T0 if empty_part_first else T0 + 6 * FRAME_DT
    healthy_start = T0 + 5 * FRAME_DT if empty_part_first else T0
    first = rec('first.mcap').add_camera(
        CAM,
        indexed_frames(5),
        keyint=5,
        drop=(0,),
        t0=empty_start,
    )
    last = rec('last.mcap').add_camera(CAM, indexed_frames(6), keyint=5, t0=healthy_start)
    eager, lazy, provider = sessions(builder, [first.write(), last.write()])
    assert [f.t_ns for f in lazy.keyframe_stream(CAM, every_n=2)] == [
        f.t_ns for f in eager.keyframe_stream(CAM, every_n=2)
    ]
    assert not provider.pinned
    new_builder = ShardCatalogBuilder()
    _, empty, provider = sessions(new_builder, [first.path])
    for session in (Session(first.path), empty):
        with pytest.raises(ValueError, match='no keyframe'):
            list(session.keyframe_stream(CAM))
    assert not provider.pinned


def test_video_failure_releases_the_shard(builder, rec):
    path = rec().add_camera(CAM, indexed_frames(3), fmt='av1').write()
    _, lazy, provider = sessions(builder, [path])
    with pytest.raises(ValueError, match='cannot locate keyframes'):
        list(lazy.keyframe_stream(CAM))
    assert not provider.pinned
    with pytest.raises(ValueError, match='unsupported sparse video codec'):
        list(lazy.keyframe_records([CAM], start_ns=T0, end_ns=T0 + 4 * FRAME_DT))
    assert not provider.pinned


def test_calibration_from_first_healthy_part(builder, stereo_calib_rec):
    paths = [stereo_calib_rec(n_frames=3, name=f'{i}.mcap')[0] for i in range(3)]
    eager, lazy, provider = sessions(builder, paths)
    assert lazy.metadata == eager.metadata
    assert sorted(lazy.calibration.cams) == sorted(eager.calibration.cams)
    for topic, cam in lazy.calibration.cams.items():
        expected = eager.calibration.cams[topic]
        np.testing.assert_array_equal(cam.K, expected.K)
        np.testing.assert_array_equal(cam.D, expected.D)
    np.testing.assert_array_equal(lazy.calibration.stereo_R, eager.calibration.stereo_R)
    np.testing.assert_array_equal(lazy.calibration.stereo_T, eager.calibration.stereo_T)
    for part in lazy.stream_summaries()[0].files:
        with lazy.part_session(part) as session:
            session.require_chunk_indexes()
            assert session.device == lazy.device
    assert not provider.pinned


@pytest.mark.parametrize('kwargs', [{'topic_mode': 'preserve'}, {'device': 'GILABS-other'}])
def test_preserved_topics_and_device_filter_parity(builder, shards, kwargs):
    eager, lazy, provider = sessions(builder, shards, **kwargs)
    assert lazy.device == eager.device and lazy.topics() == eager.topics()
    part = lazy.stream_summaries()[0].files[0]
    with lazy.part_session(part) as local:
        assert local.device == lazy.device
        assert [t.topic for t in local.topics()] == [t.topic for t in lazy.topics()]
    assert not provider.pinned


def test_part_session_keeps_ambiguous_parent_topics_unmodified(builder, rec):
    paths = [
        rec(f'{i}.mcap', device=f'GILABS-{i}')
        .add_camera(CAM, indexed_frames(2), t0=T0 + i * 3 * FRAME_DT)
        .write()
        for i in range(2)
    ]
    eager, lazy, provider = sessions(builder, paths)
    assert eager.device is lazy.device is None
    for parent in (eager, lazy):
        for i, part in enumerate(parent.stream_summaries()[0].files):
            with parent.part_session(part) as child:
                assert child.device is None
                assert [t.topic for t in child.topics()] == [f'/GILABS-{i}{CAM}']
    assert not provider.pinned


def test_explicit_unsupported_and_invalid_queries(builder, shards):
    eager, lazy, provider = sessions(builder, shards)
    with pytest.raises(UnsupportedOperationError, match='sidecars'):
        OnDemandSession(lazy.catalog, provider, sidecars=['anything'])
    with pytest.raises(UnsupportedOperationError, match='stream'):
        lazy.stream()
    for session in (eager, lazy):
        with pytest.raises(ValueError):
            session.keyframe_stream('/missing')
        with pytest.raises(ValueError):
            session.keyframe_stream(CAM, every_n=0)
        with pytest.raises(ValueError):
            session.keyframe_records([CAM], start_ns=5, end_ns=5)
    assert provider.events == []
