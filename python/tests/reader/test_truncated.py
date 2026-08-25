"""Tail-truncated MCAPs: the footer/summary never made it to disk.

A device dying (or an upload cut off) mid-write leaves a file whose chunks so
far are intact but whose footer — the LAST thing a writer emits — is absent, so
every summary-driven read raises before a single message is seen. Production
signature (data-process round1, 2 of 53 failures): RecordLengthLimitExceeded
with a ~10^19 "length" parsed out of garbage tail bytes.

These tests pin the tolerant path — index by linear scan, read the recoverable
prefix, warn — and, just as hard, everything it must NOT swallow: a bad head
magic, an environment that cannot decompress, real corruption in a file that was
merely written without an index, and `require_index`'s refusal to pay for a scan.
"""

from __future__ import annotations

import os

import pytest
from _helpers import (
    FRAME_DT,
    T0,
    frame_index_of,
    indexed_frames,
    stereo_calib_builder,
    unindexed_mcap,
)
from mcap.exceptions import InvalidMagic, UnsupportedCompressionError
from mcap.reader import FOOTER_SIZE
from mcap.records import Footer
from mcap.stream_reader import MAGIC_SIZE, StreamReader

from visio_schema.mcap.crypto import MCAP_MAGIC
from visio_schema.reader import Session

N = 8
LOGGER = "visio_schema.reader.session"


def _summary_start(path):
    """Byte offset of the summary section, read from the still-intact footer."""
    with open(path, "rb") as f:
        f.seek(-(FOOTER_SIZE + MAGIC_SIZE), os.SEEK_END)
        footer = next(StreamReader(f, skip_magic=True).records)
    assert isinstance(footer, Footer) and footer.summary_start > 0
    return footer.summary_start


def _chop_tail(path, garbage=b"", at=None):
    """Cut a file short — what a mid-write death leaves behind.

    Default cut is at the summary, so the data section (every chunk, the DataEnd
    record) stays byte-identical and only the footer is lost. `at` cuts earlier,
    landing inside a chunk. `garbage` optionally lands where the summary used to
    be, modelling a partial flush of whatever the writer was emitting when it died.
    """
    cut = _summary_start(path) if at is None else at
    path.write_bytes(path.read_bytes()[:cut] + garbage)


def _camera_rec(rec, n=N, **write_kw):
    return rec().add_camera("/ego/camera/0", indexed_frames(n)).write(**write_kw)


# ---- the recoverable prefix ------------------------------------------- #


def test_footer_chop_recovers_every_frame(rec, caplog):
    path = _camera_rec(rec)
    _chop_tail(path)
    with caplog.at_level("WARNING", logger=LOGGER):
        s = Session([path])
        frames = list(s.stream())
    assert s.truncated_files == [path]
    infos = {t.topic: t for t in s.topics()}
    assert infos["/ego/camera/0"].message_count == N
    # every frame precedes the cut, so the prefix IS the recording
    assert [frame_index_of(f.image) for f in frames] == list(range(N))
    assert "tail-truncated" in caplog.text


def test_garbage_tail_matches_production_signature(rec, caplog):
    """Round1's two corrupt uploads: opcode 0x5d + an absurd length prefix.

    Pins WHICH errors the tolerant path actually meets — the summary read trips
    on a garbage string field, and only the linear scan reaches the 4 GiB record
    cap that gave the production traceback its name.
    """
    junk = (bytes([0x5D])
            + (10_114_836_484_660_101_083).to_bytes(8, "little")
            + b"\xab" * 16)
    path = _camera_rec(rec)
    _chop_tail(path, garbage=junk)
    with caplog.at_level("DEBUG", logger=LOGGER):
        s = Session([path])
        assert len(list(s.stream())) == N
    assert s.truncated_files == [path]
    assert "UnicodeDecodeError" in caplog.text          # at the summary gate
    assert "RecordLengthLimitExceeded" in caplog.text   # in the linear scan


def test_mid_chunk_cut_keeps_a_clean_prefix(rec):
    """A cut INSIDE a chunk drops that chunk, never the intact ones before it."""
    b = rec()
    b.add_camera("/ego/camera/0", indexed_frames(24))
    b.write(chunk_size=2048)  # force several chunks so the cut falls mid-file
    _chop_tail(b.path, at=int(_summary_start(b.path) * 0.6))
    frames = list(Session([b.path]).stream())
    # deterministic fixture: a 60% cut keeps well over half the frames, and the
    # floor is what makes the contiguity assertion below mean anything
    assert 10 <= len(frames) < 24
    # a contiguous prefix: nothing skipped, nothing out of order, no partial junk
    assert [frame_index_of(f.image) for f in frames] == list(range(len(frames)))


def test_intact_chunks_plus_truncated_tail_chunk(rec):
    """The production shape: ego_0000..N intact, the LAST chunk cut mid-write."""
    a = rec("ego_0000.mcap").add_camera("/ego/camera/0", indexed_frames(6)).write()
    b = rec("ego_0001.mcap")
    b.add_camera("/ego/camera/0", indexed_frames(6), t0=T0 + 100 * FRAME_DT)
    b.write()
    _chop_tail(b.path)
    s = Session([a, b.path])
    assert s.truncated_files == [b.path]
    frames = list(s.stream())
    assert len(frames) == 12
    ts = [f.t_ns for f in frames]
    assert ts == sorted(ts)


def test_keyframe_stream_on_truncated_file(rec):
    """The sample-frames path — cadence probe + sparse decode, both footer-free."""
    b = rec()
    b.add_camera("/ego/camera/0", indexed_frames(15), keyint=5)
    b.write()
    _chop_tail(b.path)
    s = Session([b.path])
    cad = s.keyframe_cadence("/ego/camera/0")
    assert cad is not None and cad.frames_per_gop == 5
    keys = list(s.keyframe_stream("/ego/camera/0"))
    assert [frame_index_of(f.image) for f in keys] == [0, 5, 10]


def test_raw_stream_on_truncated_file(rec):
    """`raw=True` takes `_iter_file_raw`, a different seam call site."""
    b = rec()
    b.add_camera("/ego/camera/0", indexed_frames(4))
    for i in range(4):
        b.add_imu_bundle("/ego/imu/0/raw", T0 + i * FRAME_DT, offsets=(0, 5_000_000))
    b.write()
    _chop_tail(b.path)
    topics = {r.topic for r in Session([b.path]).stream(raw=True)}
    assert topics == {"/ego/camera/0", "/ego/imu/0/raw"}


def test_exposure_survives_a_footer_chop(rec):
    """`_exposure_tracks` reads CameraFrameInfo through the same seam."""
    b = rec()
    b.add_camera("/ego/camera/0", indexed_frames(N))
    b.add_frame_info("/ego/camera/0", n=N)
    b.write()
    _chop_tail(b.path)
    frames = [f for f in Session([b.path]).stream() if f.topic == "/ego/camera/0"]
    assert len(frames) == N
    assert all(f.exposure is not None for f in frames)


# ---- metadata ---------------------------------------------------------- #


def test_capture_metadata_still_readable(rec):
    # visio.capture is written at the head of the file, well before any cut
    b = rec(capture={"session_name": "s1"})
    b.add_camera("/ego/camera/0", indexed_frames(3))
    b.write()
    _chop_tail(b.path)
    md = Session([b.path]).metadata
    assert md.capture["session_name"] == "s1"
    assert md.start_ns == T0


def test_metadata_records_drains_past_the_cut(rec, caplog):
    """`metadata_records` has no early break, so unlike `_read_metadata` it is the
    one path that actually reaches the tolerant metadata reader's handler."""
    b = rec(capture={"session_name": "s1"})
    b.add_camera("/ego/camera/0", indexed_frames(3))
    b.write()
    _chop_tail(b.path)
    with caplog.at_level("DEBUG", logger=LOGGER):
        recs = Session([b.path]).metadata_records()
    assert recs == [("visio.capture", {"session_name": "s1"})]
    assert "truncation point" in caplog.text


# ---- calibration: a HALF answer must not win --------------------------- #


def test_calibration_survives_a_footer_chop(tmp_path):
    b = stereo_calib_builder(tmp_path / "rec.mcap")
    b.add_camera("/ego/camera/0", indexed_frames(6))
    b.add_camera("/ego/camera/1", indexed_frames(6))
    b.write()
    _chop_tail(b.path)
    cal = Session([b.path]).calibration
    assert sorted(cal.cams) == ["/ego/camera/0", "/ego/camera/1"]
    assert cal.cam_imu_dt_ns is not None


def test_intact_sibling_answers_calibration_before_a_truncated_file(tmp_path):
    """First-file-wins must not answer from a truncated file's PARTIAL calib when
    an intact sibling carries the whole thing."""
    a = stereo_calib_builder(tmp_path / "ego_0000.mcap")
    a.add_camera("/ego/camera/0", indexed_frames(10))
    a.add_camera("/ego/camera/1", indexed_frames(10))
    a.write(chunk_size=512)
    b = stereo_calib_builder(tmp_path / "ego_0001.mcap")
    b.add_camera("/ego/camera/0", indexed_frames(10), t0=T0 + 1000 * FRAME_DT)
    b.add_camera("/ego/camera/1", indexed_frames(10), t0=T0 + 1000 * FRAME_DT)
    b.write()
    # cut ego_0000 early enough to lose part of its calibration set
    _chop_tail(a.path, at=int(_summary_start(a.path) * 0.08))
    s = Session([a.path, b.path])
    assert s.truncated_files == [a.path]
    intact = Session([b.path]).calibration
    got = s.calibration
    assert sorted(got.cams) == sorted(intact.cams)
    assert got.cam_imu_dt_ns == intact.cam_imu_dt_ns


# ---- the scan's own bounds --------------------------------------------- #


def test_scan_bounds_are_min_max_not_first_last(rec):
    """The linear scan runs in FILE order, which need not be log-time order — so
    first/last would report a span running backwards."""
    b = rec()
    b.add_camera("/ego/camera/1", indexed_frames(4), t0=T0 + 10 * FRAME_DT)
    b.add_camera("/ego/camera/0", indexed_frames(4), t0=T0)
    b.write(sort=False)  # later stamps written FIRST
    _chop_tail(b.path)
    md = Session([b.path]).metadata
    assert md.start_ns == T0
    assert md.end_ns == T0 + 13 * FRAME_DT


# ---- what must STAY loud ----------------------------------------------- #


def test_intact_file_is_not_flagged(rec):
    path = _camera_rec(rec)
    assert Session([path]).truncated_files == []


def test_an_indexless_file_is_not_reported_as_truncated(tmp_path):
    """A file written without an index is not damaged. `truncated` is what routes
    reads to the tolerant path, so mislabelling it here desynchronises the index
    from every later read."""
    s = Session(unindexed_mcap(tmp_path / "nosum.mcap"))
    assert s.truncated_files == []
    assert [t.topic for t in s.topics()] == ["/pose/x"]


def test_corruption_in_an_indexless_file_still_raises(tmp_path):
    """The tolerant reader is reserved for a file diagnosed as truncated. On a file
    whose summary read SUCCEEDED (and merely said "no index"), the same error is
    real mid-file corruption: swallowing it would report a partial index as
    complete and leave `truncated_files` denying it."""
    from visio_schema.reader.session import _TRUNCATION_ERRORS

    path = unindexed_mcap(tmp_path / "nosum.mcap")
    raw = bytearray(path.read_bytes())
    raw[40:48] = (10_114_836_484_660_101_083).to_bytes(8, "little")
    path.write_bytes(bytes(raw))
    # the error is one the TOLERANT path would have swallowed — that it escapes
    # here is the whole claim; which member of the tuple it happens to be depends
    # on where the smashed bytes land and is not the point
    with pytest.raises(_TRUNCATION_ERRORS):
        Session([path])


def test_missing_decompression_library_is_not_truncation(rec, monkeypatch):
    """An environment that cannot decompress must not be reported as an empty
    recording: the file is fine and this process is not. Both environment errors
    subclass McapError, so only an explicit re-raise keeps them loud."""
    import visio_schema.reader.session as sess

    path = _camera_rec(rec)
    _chop_tail(path)
    real = sess.NonSeekingReader

    class _NoZstd:
        def __init__(self, f):
            self._inner = real(f)

        def iter_messages(self, *a, **kw):
            raise UnsupportedCompressionError("zstd")
            yield  # pragma: no cover — generator marker

    monkeypatch.setattr(sess, "NonSeekingReader", _NoZstd)
    with pytest.raises(UnsupportedCompressionError):
        Session([path])


def test_not_an_mcap_still_raises(tmp_path):
    """A bad HEAD magic is not truncation — the loud failure must survive."""
    bogus = tmp_path / "bogus.mcap"
    bogus.write_bytes(b"hello world, definitely not an mcap file")
    with pytest.raises(InvalidMagic):
        Session([bogus])


def test_a_file_shorter_than_a_footer_is_handled(tmp_path):
    """The MOST truncated a file can be: the header landed, nothing else did. The
    footer seek is relative to the END, so this raises EINVAL rather than anything
    MCAP-shaped — and one such tail chunk must not kill a whole session."""
    stub = tmp_path / "ego_0001.mcap"
    stub.write_bytes(MCAP_MAGIC + b"\x01\x02\x03\x04")
    s = Session([stub])
    assert s.truncated_files == [stub]
    assert s.topics() == []


def test_all_garbage_after_magic_yields_empty_index(tmp_path, caplog):
    """Nothing recoverable -> an empty session. Recovering ZERO must not read like
    success in the log, since it is the one case a caller cannot tell from a whole
    file by looking at `truncated_files`."""
    p = tmp_path / "junk.mcap"
    p.write_bytes(MCAP_MAGIC + b"\xff" * 256)
    with caplog.at_level("WARNING", logger=LOGGER):
        s = Session([p])
    assert s.truncated_files == [p]
    assert s.topics() == []
    assert "recovered NO message" in caplog.text


# ---- require_index: a preflight must stay a preflight ------------------ #


def test_require_index_still_refuses_a_truncated_file(rec):
    """`require_index` buys a METADATA-ONLY preflight: it must not silently turn
    into the full linear scan it exists to avoid, truncated or not."""
    path = _camera_rec(rec)
    _chop_tail(path)
    with pytest.raises(ValueError, match="the file is truncated"):
        Session([path], require_index=True)


def test_require_index_message_distinguishes_the_two_causes(tmp_path):
    """The wordings are disjoint, not a prefix and its extension — an operator has
    to be able to tell a damaged file from an unindexed one."""
    with pytest.raises(ValueError, match="written without an index") as exc:
        Session(unindexed_mcap(tmp_path / "nosum.mcap"), require_index=True)
    assert "the file is truncated" not in str(exc.value)
