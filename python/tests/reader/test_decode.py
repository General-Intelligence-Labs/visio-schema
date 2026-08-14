"""Decode correctness: 1-in-1-out, gap-preserving, stamp integrity (§3)."""

from __future__ import annotations

import pytest
from _helpers import FRAME_DT, T0, frame_index_of, indexed_frames

from visio_schema.reader import Frame, Session, is_keyframe


def _au(*nals: bytes) -> bytes:
    """Annex-B access unit: 4-byte start code before each NAL."""
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


# HEVC NAL headers: (type << 1) in the first byte, then the layer/tid byte.
def _hevc(nal_type: int) -> bytes:
    return bytes([nal_type << 1, 0x01]) + b"\xde\xad\xbe\xef"


VPS, SPS, PPS = _hevc(32), _hevc(33), _hevc(34)
IDR_W_RADL, TRAIL_R = _hevc(19), _hevc(1)


def test_hevc_irap_au_is_a_keyframe():
    """The real shape: parameter sets precede the slice, so the walk must skip them."""
    assert is_keyframe("h265", _au(VPS, SPS, PPS, IDR_W_RADL)) is True


def test_hevc_delta_au_is_not():
    assert is_keyframe("h265", _au(TRAIL_R)) is False


@pytest.mark.parametrize("nal_type", [16, 21])
def test_the_whole_hevc_irap_range_counts(nal_type):
    """BLA_W_LP..CRA_NUT, not just IDR_W_RADL — all are independently decodable."""
    assert is_keyframe("hevc", _au(_hevc(nal_type))) is True


@pytest.mark.parametrize("nal_type", [15, 22])
def test_the_irap_range_does_not_leak_at_either_end(nal_type):
    """15 and 22 are VCL but not IRAP — an outward off-by-one must not pass."""
    assert is_keyframe("hevc", _au(_hevc(nal_type))) is False


def test_the_first_slice_decides_not_a_later_one():
    """A trailing IRAP-numbered byte inside the payload must not flip the answer."""
    assert is_keyframe("h265", _au(SPS, TRAIL_R, IDR_W_RADL)) is False


def test_h264_idr_slice_is_a_keyframe():
    idr = bytes([0x65]) + b"\xde\xad"  # nal_ref_idc=3, type 5
    non_idr = bytes([0x41]) + b"\xde\xad"  # type 1
    assert is_keyframe("h264", _au(idr)) is True
    assert is_keyframe("avc", _au(non_idr)) is False


def test_h264_parameter_sets_are_skipped_like_hevc_ones():
    """The non-VCL skip on the H.264 side — its own branch, its own test."""
    sps, pps = bytes([0x67, 0x42]), bytes([0x68, 0xCE])
    idr = bytes([0x65]) + b"\xde\xad"
    assert is_keyframe("h264", _au(sps, pps, idr)) is True


def test_mjpeg_is_always_a_keyframe():
    assert is_keyframe("mjpeg", b"\xff\xd8\xff\xe0") is True
    assert is_keyframe("jpeg", b"\xff\xd8\xff\xe0") is True  # the alias


def test_the_format_is_matched_case_insensitively():
    """`CompressedVideo.format` is producer-written text, not an enum."""
    assert is_keyframe("H265", _au(VPS, SPS, PPS, IDR_W_RADL)) is True
    assert is_keyframe("MJPEG", b"\xff\xd8") is True


def test_a_slice_past_the_prefix_bound_is_not_claimed_as_a_keyframe():
    """The bound is real — it is passed to `find`, not merely compared against.

    Without it a malformed AU is scanned to its last byte, which is the 1.86 s /
    900 AUs pathology the whole peek exists to avoid.
    """
    from visio_schema.reader._decode import _MAX_PREFIX_BYTES

    buried = b"\x00" * (_MAX_PREFIX_BYTES + 1024) + _au(IDR_W_RADL)
    assert is_keyframe("h265", buried) is False


def test_an_unparsed_codec_reports_none_not_false():
    """None is 'no parser', a property of the stream — the caller falls back once."""
    assert is_keyframe("av1", _au(IDR_W_RADL)) is None
    assert is_keyframe("vp9", b"whatever") is None


def test_garbage_with_no_slice_is_not_a_keyframe():
    """A parsable codec always gets a definite answer: nothing to feed on its own."""
    assert is_keyframe("h265", b"") is False
    assert is_keyframe("h265", b"\x11" * 64) is False
    assert is_keyframe("h265", _au(VPS, SPS)) is False


def test_frames_stamped_by_capture_pts(rec):
    b = rec()
    b.add_camera("/ego/camera/0", indexed_frames(6))
    b.write()
    frames = list(Session([b.path]).stream())
    assert len(frames) == 6
    for f in frames:
        assert isinstance(f, Frame)
        idx = frame_index_of(f.image)
        assert f.t_ns == T0 + idx * FRAME_DT  # each frame keeps its own PTS
    ts = [f.t_ns for f in frames]
    assert ts == sorted(ts)


def test_dropped_au_leaves_gap_no_drift(rec):
    b = rec()
    b.add_camera("/ego/camera/0", indexed_frames(6), drop={3})
    b.write()
    frames = list(Session([b.path]).stream())
    got = {frame_index_of(f.image): f.t_ns for f in frames}
    assert set(got) == {0, 1, 2, 4, 5}  # AU 3 dropped, no shift
    for idx, t in got.items():
        assert t == T0 + idx * FRAME_DT  # survivors keep their true PTS


def test_gray_decode_path(rec):
    b = rec()
    b.add_camera("/ego/camera/0", indexed_frames(3))
    b.write()
    frames = list(Session([b.path]).stream(gray=True))
    assert len(frames) == 3
    assert all(f.is_gray and f.image.ndim == 2 for f in frames)


def test_time_window(rec):
    b = rec()
    b.add_camera("/ego/camera/0", indexed_frames(6))
    b.write()
    lo, hi = T0 + 2 * FRAME_DT, T0 + 5 * FRAME_DT
    frames = list(Session([b.path]).stream(start_ns=lo, end_ns=hi))
    idxs = sorted(frame_index_of(f.image) for f in frames)
    assert idxs == [2, 3, 4]  # [start, end)
