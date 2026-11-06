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
    BlobReaderIO,
    OnDemandSession,
    Session,
    ShardCatalog,
    ShardCatalogBuilder,
    ShardRef,
    SummaryUnavailableError,
    UnsupportedOperationError,
)
from visio_schema.reader.session import _FileIndex

CAM = '/ego/camera/0'


class MemoryBlob:
    def __init__(self, data):
        self.data, self.calls = data, []

    @property
    def size(self):
        return len(self.data)

    def read_at(self, offset, size):
        self.calls.append((offset, size))
        return self.data[offset : offset + size]


class LocalProvider:
    def __init__(self, paths):
        self.paths = {p.name: p for p in paths}
        self.pinned, self.events = set(), []
        self.peak = 0

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
    return ShardCatalogBuilder(
        episode_id='episode', source_uri='s3://test/ingested/', created_by='test'
    )


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
    catalog = builder.build(
        [builder.entry_from_blob(p.name, MemoryBlob(p.read_bytes())) for p in reversed(shards)]
    )
    provider = LocalProvider(shards)
    return Session(shards, **kwargs), OnDemandSession(catalog, provider, **kwargs), provider


def test_catalog_roundtrip_and_inventory(builder, shards, tmp_path):
    eager, lazy, provider = sessions(builder, shards)
    catalog = ShardCatalog.from_json(json.loads(json.dumps(lazy.catalog.to_json())))
    assert catalog == lazy.catalog
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
    data = builder.build([builder.entry_from_local_file(shards[0].name, shards[0])]).to_json()
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
    data = builder.build([]).to_json()
    data['version'] = version
    with pytest.raises(ValueError, match='version'):
        ShardCatalog.from_json(data)


def test_blob_adapter_seek_and_close():
    blob = MemoryBlob(b'abcdefgh')
    with BlobReaderIO(blob) as file:
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


def test_range_summary_uses_bounded_reads(builder, rec):
    path = rec().add_camera(CAM, indexed_frames(20)).write(chunk_size=200)
    blob = MemoryBlob(path.read_bytes())
    entry = builder.entry_from_blob('remote.mcap', blob)
    assert entry.index_source == 'range_summary'
    assert entry.messages == 20 and entry.chunk_indexed and not entry.truncated
    assert entry.start_ns == T0 and entry.end_ns == T0 + 19 * FRAME_DT
    assert entry.topic_info[0].schema_name == 'foxglove.CompressedVideo'
    cut = _summary_start(path)
    assert all(offset < 8 or offset >= cut for offset, _size in blob.calls)
    assert sum(size for _offset, size in blob.calls) < len(blob.data) // 2
    assert len(blob.calls) < 20


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


def test_missing_summary_requires_explicit_local_fallback(builder, tmp_path):
    path = unindexed_mcap(tmp_path / 'no-summary.mcap')
    with pytest.raises(SummaryUnavailableError, match='without an index'):
        builder.entry_from_blob(path.name, MemoryBlob(path.read_bytes()))
    entry = builder.entry_from_local_file(path.name, path)
    assert entry.index_source == 'download_scan'
    assert not entry.chunk_indexed and not entry.truncated
    assert entry.metadata_names is None and entry.messages > 0


def test_unreadable_summary_and_truncated_prefix(builder, rec):
    path = rec().add_camera(CAM, indexed_frames(10)).write(chunk_size=200)
    _chop_tail(path)
    with pytest.raises(SummaryUnavailableError, match='truncated'):
        builder.entry_from_blob(path.name, MemoryBlob(path.read_bytes()))
    entry = builder.entry_from_local_file(path.name, path)
    assert entry.truncated and not entry.chunk_indexed
    assert entry.index_source == 'download_scan' and entry.messages == 10
    provider = LocalProvider([path])
    lazy = OnDemandSession(builder.build([entry]), provider)
    assert len(list(lazy.keyframe_stream(CAM))) == 10
    with pytest.raises(ValueError, match='chunk indexes'):
        lazy.require_chunk_indexes()
    with pytest.raises(SummaryUnavailableError):
        OnDemandSession(lazy.catalog, provider, require_index=True)
    assert not provider.pinned


def test_invalid_magic_and_network_error_do_not_trigger_fallback(builder):
    with pytest.raises(InvalidMagic):
        builder.entry_from_blob('not.mcap', MemoryBlob(b'not mcap' * 10))

    class FailingBlob(MemoryBlob):
        def read_at(self, offset, size):
            if offset > 8:
                raise PermissionError('denied')
            return super().read_at(offset, size)

    with pytest.raises(PermissionError, match='denied'):
        builder.entry_from_blob('network.mcap', FailingBlob(b'\x89MCAP0\r\n' + b'x' * 100))


def test_corrupt_summary_requires_explicit_fallback(builder, rec):
    path = rec().add_camera(CAM, indexed_frames(4)).write()
    data = bytearray(path.read_bytes())
    start = _summary_start(path)
    data[start + 1 : start + 9] = (2**63).to_bytes(8, 'little')
    path.write_bytes(data)
    with pytest.raises(SummaryUnavailableError):
        builder.entry_from_blob(path.name, MemoryBlob(data))
    entry = builder.entry_from_local_file(path.name, path)
    assert entry.index_source == 'download_scan'
    assert entry.truncated and not entry.chunk_indexed and entry.messages == 4


def test_short_remote_read_is_an_io_error_not_summary_fallback(builder, shards):
    class ShortBlob(MemoryBlob):
        def read_at(self, offset, size):
            return super().read_at(offset, size)[:-1]

    with pytest.raises(OSError, match='short read'):
        builder.entry_from_blob('short.mcap', ShortBlob(shards[0].read_bytes()))


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
    entry = builder.entry_from_blob(b.path.name, MemoryBlob(b.path.read_bytes()))
    assert entry.index_source == 'range_summary' and not entry.chunk_indexed
    lazy = OnDemandSession(builder.build([entry]), LocalProvider([b.path]), require_index=True)
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


def test_no_keyframes_in_first_shard_does_not_fail_a_healthy_session(builder, rec):
    first = rec('first.mcap').add_camera(CAM, indexed_frames(5), keyint=5, drop=(0,))
    last = rec('last.mcap').add_camera(CAM, indexed_frames(6), keyint=5, t0=T0 + 5 * FRAME_DT)
    eager, lazy, provider = sessions(builder, [first.write(), last.write()])
    assert [f.t_ns for f in lazy.keyframe_stream(CAM, every_n=2)] == [
        f.t_ns for f in eager.keyframe_stream(CAM, every_n=2)
    ]
    assert not provider.pinned
    _, empty, provider = sessions(builder, [first.path])
    with pytest.raises(ValueError, match='no keyframe'):
        list(empty.keyframe_stream(CAM))
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


def test_preserved_topics_and_device_filter_parity(builder, shards):
    for kwargs in ({'topic_mode': 'preserve'}, {'device': 'GILABS-other'}):
        eager, lazy, provider = sessions(builder, shards, **kwargs)
        assert lazy.device == eager.device and lazy.topics() == eager.topics()
        part = lazy.stream_summaries()[0].files[0]
        with lazy.part_session(part) as local:
            assert local.device == lazy.device
            assert [t.topic for t in local.topics()] == [t.topic for t in lazy.topics()]
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
