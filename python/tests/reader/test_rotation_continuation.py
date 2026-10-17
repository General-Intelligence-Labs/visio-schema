"""A part seam must not reset the reference pictures of a continuing camera."""

import sys

import numpy as np
import pytest
from _helpers import RecBuilder, indexed_frames
from mcap.writer import Writer

from visio_schema import make_channel
from visio_schema.reader import Session, sync


def write_rows(path, rows, part, recording="test-recording", metadata=True):
    with path.open("wb") as f:
        w = Writer(f)
        w.start()
        if metadata:
            w.add_metadata(
                "visio.rotation",
                {
                    "version": "1",
                    "recording_id": recording,
                    "part_index": str(part),
                    "video_continuation": "true",
                    "cut_reason": "initial" if part == 0 else "forced",
                },
            )
        channels = {}
        for topic, schema_name, t, payload, seq in rows:
            if topic not in channels:
                ch = make_channel(topic, schema_name, stream_id=16)
                sid = w.register_schema(name=schema_name, encoding="protobuf", data=ch.schema)
                channels[topic] = w.register_channel(
                    topic=topic, message_encoding="protobuf", schema_id=sid
                )
            w.add_message(
                channel_id=channels[topic], log_time=t, publish_time=t, data=payload, sequence=seq
            )
        w.finish()
    return path


def fixture_rows(tmp_path, *, h=64, w=96):
    builder = RecBuilder(tmp_path / "unused.mcap")
    frames = indexed_frames(16, h=h, w=w)
    builder.add_camera("/ego/camera/0", frames, keyint=12)
    builder.add_camera("/wrist/camera/0", frames, keyint=12, skew=8000)
    counters = {}
    rows = []
    for topic, schema, t, data in sorted(builder._rows, key=lambda r: r[2]):
        seq = counters.get(topic, 0)
        counters[topic] = seq + 1
        rows.append((topic, schema, t, data, seq))
    return rows


def test_split_non_idr_continuation_matches_unsplit_decode_and_pairing(tmp_path):
    rows = fixture_rows(tmp_path)
    whole = write_rows(tmp_path / "whole.mcap", rows, 0)
    split = [
        write_rows(tmp_path / f"part_{i}.mcap", [r for r in rows if lo <= r[4] < hi], i)
        for i, (lo, hi) in enumerate([(0, 5), (5, 9), (9, 16)])
    ]
    expected = list(Session([whole]).stream())
    actual = list(Session(split).stream())
    assert [(x.topic, x.t_ns) for x in actual] == [(x.topic, x.t_ns) for x in expected]
    for a, b in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(a.image, b.image)
    keys = sorted({x.topic for x in expected})

    def paired(frames):
        return [(g.t_ns, g.residual_ns) for g in sync(frames, keys, tol_ns=1_000_000)]

    assert paired(actual) == paired(expected)


def test_missing_part_never_reuses_prior_reference_pictures(tmp_path):
    rows = fixture_rows(tmp_path)
    a = write_rows(tmp_path / "a.mcap", [r for r in rows if r[4] < 5], 0)
    b = write_rows(tmp_path / "b.mcap", [r for r in rows if r[4] >= 9], 2)
    decoded = list(Session([a, b]).stream())
    # The second file begins before the next independently decodable frame.
    for topic in {r[0] for r in rows}:
        assert sum(x.topic == topic for x in decoded) == 9


@pytest.mark.parametrize("break_kind", ["identity", "sequence", "frame_id"])
def test_discontinuity_waits_for_next_real_idr(tmp_path, break_kind):
    from visio_schema.foxglove.CompressedVideo_pb2 import CompressedVideo

    rows = fixture_rows(tmp_path)
    first = [r for r in rows if r[4] < 5]
    rest = [r for r in rows if r[4] >= 5]
    if break_kind == "sequence":
        rest = [(*r[:4], r[4] + 100 if r[0] == "/wrist/camera/0" else r[4]) for r in rest]
    if break_kind == "frame_id":
        changed = []
        for topic, schema, t, payload, seq in rest:
            video = CompressedVideo.FromString(payload)
            if topic == "/wrist/camera/0":
                video.frame_id = "replacement"
            changed.append((topic, schema, t, video.SerializeToString(), seq))
        rest = changed
    a = write_rows(tmp_path / "a.mcap", first, 0)
    b = write_rows(
        tmp_path / "b.mcap",
        rest,
        1,
        recording="other" if break_kind == "identity" else "test-recording",
    )
    actual = list(Session([a, b]).stream())
    for topic in {r[0] for r in rows}:
        expected = 9 if break_kind == "identity" or topic == "/wrist/camera/0" else 16
        assert sum(x.topic == topic for x in actual) == expected


def test_many_parts_reuse_only_codec_contexts(tmp_path, monkeypatch):
    import visio_schema.reader.rows as module

    original = module.HevcDecoder
    made = []

    def create(*args, **kwargs):
        decoder = original(*args, **kwargs)
        made.append(decoder)
        return decoder

    monkeypatch.setattr(module, "HevcDecoder", create)
    rows = fixture_rows(tmp_path)
    parts = [
        write_rows(tmp_path / f"part{i:03}.mcap", [r for r in rows if r[4] == i], i)
        for i in range(16)
    ]
    assert len(list(Session(parts).stream())) == 32
    assert len(made) == 2


def test_delayed_gpu_tail_is_flushed_once_at_chain_end(tmp_path, monkeypatch):
    from visio_schema.reader import _gpu_decode

    class Delayed:
        def __init__(self, fmt):
            self.pending = []

        def decode(self, data, t):
            self.pending.append((t, np.zeros((2, 2, 3), np.uint8), None))
            return [self.pending.pop(0)] if len(self.pending) > 2 else []

        def flush(self):
            out, self.pending = self.pending, []
            return out

    monkeypatch.setattr(_gpu_decode, "NvDecoder", Delayed)
    rows = fixture_rows(tmp_path)
    whole = write_rows(tmp_path / "whole.mcap", rows, 0)
    parts = [
        write_rows(tmp_path / f"part{i}.mcap", [r for r in rows if lo <= r[4] < hi], i)
        for i, (lo, hi) in enumerate([(0, 5), (5, 9), (9, 16)])
    ]
    expected = list(Session([whole]).stream(gpu=True))
    actual = list(Session(parts).stream(gpu=True))
    assert [(x.topic, x.t_ns, x.frame_id) for x in actual] == [
        (x.topic, x.t_ns, x.frame_id) for x in expected
    ]
    assert len(actual) == 32


def test_chain_order_uses_part_identity_despite_older_remote_timestamp(tmp_path):
    rows = fixture_rows(tmp_path)
    first = write_rows(tmp_path / "a.mcap", [r for r in rows if r[4] < 5], 0)
    rest = [r for r in rows if r[4] >= 5]
    # The bus-corrected timestamp differs from the remote payload's raw clock.
    t0 = rows[0][2]
    rest.append(("/wrist/imu/0/raw", "visio_schema.v1.sensor.ImuRaw", t0 - 1000, b"", 0))
    second = write_rows(tmp_path / "b.mcap", rest, 1)
    frames = list(Session([second, first]).stream(topics=["/ego/camera/0", "/wrist/camera/0"]))
    assert len(frames) == 32
    assert [f.t_ns for f in frames] == [r[2] for r in rows]


def test_sequence_wrap_preserves_context(tmp_path):
    rows = fixture_rows(tmp_path)
    rows = [(*r[:4], (r[4] + 2**32 - 5) % 2**32) for r in rows]
    a = write_rows(tmp_path / "a.mcap", rows[:10], 0)
    b = write_rows(tmp_path / "b.mcap", rows[10:], 1)
    assert len(list(Session([a, b]).stream())) == 32


@pytest.mark.parametrize("part,identity", [("bad", "take"), (-1, "take"), (0, "")])
def test_invalid_continuation_identity_is_rejected(tmp_path, part, identity):
    rows = fixture_rows(tmp_path)
    path = write_rows(tmp_path / "bad.mcap", rows, part, recording=identity)
    with pytest.raises(ValueError, match="rotation metadata"):
        Session([path])


def test_legacy_part_does_not_seed_a_marked_continuation(tmp_path):
    rows = fixture_rows(tmp_path)
    a = write_rows(tmp_path / "legacy.mcap", [r for r in rows if r[4] < 5], 0, metadata=False)
    b = write_rows(tmp_path / "marked.mcap", [r for r in rows if r[4] >= 5], 1)
    assert len(list(Session([a, b]).stream())) == 18


def test_real_nvdec_split_matches_unsplit_frames_and_pairs(tmp_path):
    cupy = pytest.importorskip("cupy", reason="real NVDEC validation requires the GPU environment")
    pytest.importorskip("PyNvVideoCodec", reason="real NVDEC validation requires codec bindings")
    rows = fixture_rows(tmp_path, h=256, w=384)
    whole = write_rows(tmp_path / "whole.mcap", rows, 0)
    parts = [
        write_rows(tmp_path / f"part{i}.mcap", [r for r in rows if lo <= r[4] < hi], i)
        for i, (lo, hi) in enumerate([(0, 5), (5, 9), (9, 16)])
    ]
    cpu = list(Session([whole]).stream())
    expected = list(Session([whole]).stream(gpu=True))
    actual = list(Session(parts).stream(gpu=True))

    def stamps(frames):
        return [(f.topic, f.t_ns) for f in frames]

    assert stamps(actual) == stamps(expected) == stamps(cpu)
    cupy.cuda.runtime.deviceSynchronize()
    for a, b in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(cupy.asnumpy(a.image), cupy.asnumpy(b.image))
    topics = sorted({f.topic for f in actual})

    def paired(frames):
        return [(g.t_ns, g.residual_ns) for g in sync(frames, topics, tol_ns=1_000_000)]

    assert paired(actual) == paired(expected) == paired(cpu)


@pytest.mark.skipif(
    sys.platform != "linux", reason="Current RSS measurement requires Linux /proc/self/statm"
)
def test_many_large_parts_do_not_retain_protobuf_parse_arenas(tmp_path):
    import gc
    import os

    rows = fixture_rows(tmp_path)
    initial = [r for r in rows if r[4] == 0]
    # Unknown length-delimited protobuf field: valid wire data whose retained
    # parse arena is large enough to expose per-part retention in RSS.
    padding = b"\xa2\x06\x80\x80\x04" + b"p" * 65536
    paths = []
    for i in range(300):
        current = [
            (topic, schema, t + i * 33333333, payload + padding, i)
            for topic, schema, t, payload, _seq in initial
        ]
        paths.append(write_rows(tmp_path / f"part{i:04}.mcap", current, i))
    source = Session(paths).stream()

    def rss():
        with open("/proc/self/statm") as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")

    for _ in range(200):
        next(source)
    gc.collect()
    baseline = rss()
    count = 200 + sum(1 for _ in source)
    gc.collect()
    assert count == 600
    assert rss() - baseline < 12 * 1024 * 1024


def test_custom_video_adapter_keeps_per_file_flush_contract(tmp_path):
    from visio_schema.reader.domain import VIDEO_SCHEMA, Record

    flushed = []

    class Buffered:
        def __init__(self, _schema_name, ctx):
            self.pending = []

        def emit(self, payload, topic, t_ns):
            self.pending.append((t_ns, t_ns, Record(topic, t_ns, VIDEO_SCHEMA, None, payload)))
            return iter(())

        def flush(self):
            flushed.append(len(self.pending))
            return iter(self.pending)

    rows = fixture_rows(tmp_path)
    parts = [
        write_rows(tmp_path / f"part{i}.mcap", [r for r in rows if lo <= r[4] < hi], i)
        for i, (lo, hi) in enumerate([(0, 5), (5, 16)])
    ]
    decoded = list(Session(parts, adapters={VIDEO_SCHEMA: Buffered}).stream())
    assert len(decoded) == 32
    assert flushed == [10, 22]


@pytest.mark.parametrize("gpu", [False, True])
def test_parameter_sets_on_p_frame_do_not_prime_a_reset_decoder(tmp_path, gpu):
    import re

    from visio_schema.foxglove.CompressedVideo_pb2 import CompressedVideo

    if gpu:
        pytest.importorskip("cupy", reason="NVDEC requires the GPU environment")
        pytest.importorskip("PyNvVideoCodec", reason="NVDEC requires codec bindings")
    rows = fixture_rows(tmp_path, h=256, w=384)
    initial = CompressedVideo.FromString(next(r[3] for r in rows if r[0] == "/wrist/camera/0"))
    starts = list(re.finditer(rb"\x00\x00\x00?\x01", initial.data))
    prefix = b"".join(
        initial.data[
            m.start() : starts[i + 1].start() if i + 1 < len(starts) else len(initial.data)
        ]
        for i, m in enumerate(starts)
        if (initial.data[m.end()] >> 1) & 63 in {32, 33, 34}
    )
    assert prefix
    first = write_rows(tmp_path / "a.mcap", [r for r in rows if r[4] < 5], 0)
    rest = []
    for topic, schema, t, payload, seq in rows:
        if seq < 5:
            continue
        if topic == "/wrist/camera/0":
            if seq == 5:
                video = CompressedVideo.FromString(payload)
                video.data = prefix + video.data
                payload = video.SerializeToString()
            seq += 100
        rest.append((topic, schema, t, payload, seq))
    second = write_rows(tmp_path / "b.mcap", rest, 1)
    frames = list(Session([first, second]).stream(gpu=gpu))
    assert sum(f.topic == "/ego/camera/0" for f in frames) == 16
    assert sum(f.topic == "/wrist/camera/0" for f in frames) == 9
