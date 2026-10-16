"""A bounded `stream()` window skips whole shards OUTSIDE it, at both ends.

The end bound already stopped early; the start bound did not, so a short window
late in a long sharded recording opened and decoded every preceding shard only
for `_in_window` to drop it. These tests pin the file-level skip on the start
side and prove it keeps the file that actually holds the window.
"""

from __future__ import annotations

from _helpers import FRAME_DT, T0, indexed_frames

from visio_schema.reader import Frame, Session

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
