"""Incremental consumers share sync's matching and bounded frame lifetime."""

import numpy as np
import pytest

from visio_schema.reader import Frame, sync
from visio_schema.reader.ops import StreamMatcher


def test_incremental_matches_sync_and_releases_tail():
    frames = [
        Frame(topic, t, np.zeros((2, 2), np.uint8))
        for topic, t in [
            ("/a", 0),
            ("/b", 100),
            ("/a", 1000),
            ("/a", 2000),
            ("/b", 2100),
            ("/b", 3000),
        ]
    ]
    matcher = StreamMatcher(["/a", "/b"], tol_ns=100, window=4, emit_partial=True)
    emitted = [item for frame in frames for item in matcher.push(frame)]
    assert len(list(matcher.buffered_elements())) == 1
    emitted.extend(matcher.finish())
    assert list(matcher.buffered_elements()) == []
    groups = [item for item in emitted if not isinstance(item, Frame)]
    expected = list(sync(iter(frames), ["/a", "/b"], tol_ns=100))
    assert [x.t_ns for x in groups] == [x.t_ns for x in expected]
    assert sorted(x.t_ns for x in emitted if isinstance(x, Frame)) == [1000, 3000]


def test_bound_and_cancelled_finish_release_frames():
    matcher = StreamMatcher(["/a", "/b"], tol_ns=0, window=2, emit_partial=True)
    for t in range(4):
        list(matcher.push(Frame("/a", t, np.zeros((2, 2), np.uint8))))
    assert len(list(matcher.buffered_elements())) == 2
    ending = matcher.finish()
    next(ending)
    ending.close()
    assert matcher.clear() == 0


@pytest.mark.parametrize(
    "keys,window,tolerance", [([], 1, 0), (["a", "a"], 1, 0), (["a"], 0, 0), (["a"], 1, -1)]
)
def test_invalid_matcher_settings(keys, window, tolerance):
    with pytest.raises(ValueError):
        StreamMatcher(keys, tol_ns=tolerance, window=window, emit_partial=False)
