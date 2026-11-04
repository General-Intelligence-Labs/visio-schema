"""A bounded `stream()` window skips whole shards OUTSIDE it, at both ends.

The end bound already stopped early; the start bound did not, so a short window
late in a long sharded recording opened and decoded every preceding shard only
for `_in_window` to drop it. These tests pin the file-level skip on the start
side and prove it keeps the file that actually holds the window.
"""

from __future__ import annotations

import numpy as np
import pytest
from _helpers import FRAME_DT, QUAT, T0, indexed_frames

from visio_schema.reader import Frame, ImuSample, Record, Session

CAM = "/ego/camera/0"
GAP = 100_000 * FRAME_DT  # shards sit far apart so the window touches exactly one


def _shard(rec, name, t0, n=5):
    return rec(name).add_camera(CAM, indexed_frames(n), t0=t0).write()


def _spy_opens(monkeypatch):
    opened: list[str] = []
    orig = Session._iter_file

    def spy(self, path, *a, **k):
        opened.append(path.name)
        return orig(self, path, *a, **k)

    monkeypatch.setattr(Session, "_iter_file", spy)
    return opened


def test_stream_skips_shards_before_the_window(rec, monkeypatch):
    f0 = _shard(rec, "s0.mcap", T0)
    f1 = _shard(rec, "s1.mcap", T0 + GAP)
    f2 = _shard(rec, "s2.mcap", T0 + 2 * GAP)
    opened = _spy_opens(monkeypatch)

    # Window wholly inside the LAST shard: its frames 1..3 (t < end).
    start = T0 + 2 * GAP + 1 * FRAME_DT
    end = T0 + 2 * GAP + 4 * FRAME_DT
    frames = [
        e
        for e in Session([f0, f1, f2]).stream((CAM,), start_ns=start, end_ns=end)
        if isinstance(e, Frame)
    ]

    # Right content: only the last shard's in-window frames, in order.
    assert [f.t_ns for f in frames] == [start + i * FRAME_DT for i in range(3)]
    # The two leading shards were never opened; only the one that holds the window.
    assert "s0.mcap" not in opened
    assert "s1.mcap" not in opened
    assert "s2.mcap" in opened


def test_stream_keeps_the_shard_that_holds_the_window_start(rec, monkeypatch):
    """The file CONTAINING start_ns is read from its head (mid-GOP-safe), so a
    window straddling a shard boundary loses nothing — only the shard entirely
    before it is skipped."""
    f0 = _shard(rec, "s0.mcap", T0)
    f1 = _shard(rec, "s1.mcap", T0 + GAP)
    opened = _spy_opens(monkeypatch)

    # Start inside f1, end past its last frame: expect f1's frames 2..4.
    start = T0 + GAP + 2 * FRAME_DT
    end = T0 + GAP + 10 * FRAME_DT
    frames = [
        e
        for e in Session([f0, f1]).stream((CAM,), start_ns=start, end_ns=end)
        if isinstance(e, Frame)
    ]

    assert [f.t_ns for f in frames] == [start + i * FRAME_DT for i in range(3)]
    assert "s0.mcap" not in opened
    assert "s1.mcap" in opened


@pytest.mark.parametrize("raw", [False, True, {CAM}, {"/ego/imu/0/raw"}])
def test_bounded_window_preserves_wide_imu_without_decoding_old_video(rec, monkeypatch, raw):
    imu = "/ego/imu/0/raw"
    start = T0 + 2_000_000_000
    end = start + 2 * FRAME_DT
    early = rec("early.mcap").add_camera(CAM, indexed_frames(3))
    early.add_imu_bundle(imu, T0, [0, start - T0, start - T0 + FRAME_DT, end - T0])
    late = rec("late.mcap").add_camera(CAM, indexed_frames(3), t0=start)
    session = Session([early.write(), late.write()])
    expected = [e for e in session.stream([CAM, imu], raw=raw) if start <= e.t_ns < end]
    reads = []
    original = session._read_messages

    def read(path, **kwargs):
        reads.append((path.name, set(kwargs.get("topics") or ())))
        return original(path, **kwargs)

    monkeypatch.setattr(session, "_read_messages", read)
    actual = list(session.stream([CAM, imu], start_ns=start, end_ns=end, raw=raw))
    assert [(type(e), e.topic, e.t_ns) for e in actual] == [
        (type(e), e.topic, e.t_ns) for e in expected]
    for a, b in zip(actual, expected, strict=True):
        if isinstance(a, Frame):
            np.testing.assert_array_equal(a.image, b.image)
        elif isinstance(a, ImuSample):
            np.testing.assert_array_equal(a.gyro, b.gyro)
            np.testing.assert_array_equal(a.accel, b.accel)
        else:
            assert isinstance(a, Record) and a.data == b.data
    decoded_imu = raw is False or raw == {CAM}
    assert [e.t_ns for e in actual if isinstance(e, ImuSample)] == (
        [start, start + FRAME_DT] if decoded_imu else [])
    assert [topics for name, topics in reads if name == "early.mcap"] == (
        [{imu}] if decoded_imu else [])


def test_bounded_window_inside_a_single_wide_imu_bundle(rec):
    imu = "/ego/imu/0/raw"
    path = rec().add_imu_bundle(imu, T0, [0, 2_000_000_000, 3_000_000_000]).write()
    samples = list(Session([path]).stream(
        [imu], start_ns=T0 + 2_000_000_000, end_ns=T0 + 3_000_000_000))
    assert len(samples) == 1
    assert isinstance(samples[0], ImuSample)
    assert samples[0].t_ns == T0 + 2_000_000_000


def test_bounded_window_retains_custom_adapter_expansion_without_old_video(rec, monkeypatch):
    topic = "/ego/imu/0/orientation"
    start = T0 + 2_000_000_000
    early = rec("early.mcap").add_camera(CAM, indexed_frames(2))
    early.add_quat(topic, n=1)
    late = rec("late.mcap").add_camera(CAM, indexed_frames(2), t0=start)

    class Shifted:
        def __init__(self, schema_name, _ctx):
            self.schema_name = schema_name

        def emit(self, payload, topic, t_ns):
            stamp = t_ns + 2_000_000_000
            yield stamp, t_ns, Record(topic, stamp, self.schema_name, None, payload)

        def flush(self):
            return iter(())

    session = Session([early.write(), late.write()], adapters={QUAT: Shifted})
    selected = [CAM, topic]
    expected = [e for e in session.stream(selected) if start <= e.t_ns < start + FRAME_DT]
    reads = []
    original = session._read_messages

    def read(path, **kwargs):
        reads.append((path.name, set(kwargs.get("topics") or ())))
        return original(path, **kwargs)

    monkeypatch.setattr(session, "_read_messages", read)
    actual = list(session.stream(selected, start_ns=start, end_ns=start + FRAME_DT))
    assert [(e.topic, e.t_ns) for e in actual] == [(topic, start), (CAM, start)]
    for a, b in zip(actual, expected, strict=True):
        if isinstance(a, Frame):
            np.testing.assert_array_equal(a.image, b.image)
        else:
            assert a.data == b.data
    assert [topics for name, topics in reads if name == "early.mcap"] == [{topic}]
