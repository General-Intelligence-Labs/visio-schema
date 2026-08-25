"""`keyframe_cadence` + `keyframe_stream` — sparse sampling with no P-frame decode.

The fixtures here set `keyint` deliberately: under `_helpers`' all-intra default
every frame is a keyframe, so a sampler that reads the bitstream and one that
ignores it look identical. A real GOP is what makes the claims testable.
"""

from __future__ import annotations

import numpy as np
import pytest
from _helpers import FRAME_DT, T0, indexed_frames

from visio_schema.reader import Frame, HevcDecoder, Session
from visio_schema.reader import session as session_mod

CAM = "/ego/camera/0"
KEYINT = 5
N = 20
KEY_IDX = (0, 5, 10, 15)  # where an IDR lands in a 20-frame keyint=5 clip


def _stamps(frames):
    return [f.t_ns for f in frames]


def _at(*indices):
    return [T0 + i * FRAME_DT for i in indices]


@pytest.fixture
def gop_rec(rec):
    b = rec()
    b.add_camera(CAM, indexed_frames(N), keyint=KEYINT)
    return b.write()


# --- cadence ------------------------------------------------------------- #
def test_cadence_is_measured_off_the_bitstream(gop_rec):
    cad = Session([gop_rec]).keyframe_cadence(CAM)
    assert cad.topic == CAM
    assert cad.frames_per_gop == KEYINT
    assert cad.period_ns == KEYINT * FRAME_DT
    assert cad.sampled == len(KEY_IDX)


def test_cadence_of_an_all_intra_clip_is_one_frame(rec):
    """The default fixture shape — every frame independently decodable."""
    b = rec()
    b.add_camera(CAM, indexed_frames(6))
    cad = Session([b.write()]).keyframe_cadence(CAM)
    assert cad.frames_per_gop == 1
    assert cad.period_ns == FRAME_DT


def test_cadence_is_none_for_a_clip_shorter_than_one_gop(rec):
    """One keyframe gives no spacing to measure — the caller falls back to n=1."""
    b = rec()
    b.add_camera(CAM, indexed_frames(3), keyint=10)
    assert Session([b.write()]).keyframe_cadence(CAM) is None


def test_the_probe_stops_at_its_keyframe_budget(rec):
    """Bounded by construction — otherwise the cap could vanish unnoticed."""
    b = rec()
    b.add_camera(CAM, indexed_frames(20), keyint=1)  # 20 keyframes available
    cad = Session([b.write()]).keyframe_cadence(CAM)
    assert cad.sampled == session_mod._CADENCE_KEYFRAMES


def test_the_probe_gives_up_rather_than_reading_a_whole_file(rec, monkeypatch):
    """The escape for a stream whose keyframes are far apart, or absent."""
    monkeypatch.setattr(session_mod, "_CADENCE_AUS", 3)
    b = rec()
    b.add_camera(CAM, indexed_frames(20), keyint=10)  # keyframes at 0 and 10
    assert Session([b.write()]).keyframe_cadence(CAM) is None


def test_the_cadence_is_cached_including_a_none(rec, monkeypatch):
    """A `None` must cache too, or an unmeasurable topic re-probes on every call."""
    b = rec()
    b.add_camera(CAM, indexed_frames(3), keyint=10)
    sess = Session([b.write()])
    calls = []
    real = Session._measure_cadence
    monkeypatch.setattr(
        Session, "_measure_cadence",
        lambda self, t: (calls.append(t), real(self, t))[1],
    )
    assert sess.keyframe_cadence(CAM) is None
    assert sess.keyframe_cadence(CAM) is None
    assert len(calls) == 1


def test_an_unparsable_codec_is_refused_by_both_entry_points(rec):
    """`None` from `is_keyframe` is a property of the CODEC, so both must agree."""
    b = rec()
    b.add_camera(CAM, indexed_frames(6), fmt="av1")
    path = b.write()
    with pytest.raises(ValueError, match="cannot locate keyframes in 'av1'"):
        Session([path]).keyframe_cadence(CAM)
    with pytest.raises(ValueError, match="Session.stream"):
        list(Session([path]).keyframe_stream(CAM))


# --- the sparse read ----------------------------------------------------- #
def test_yields_the_keyframes_and_nothing_else(gop_rec):
    frames = list(Session([gop_rec]).keyframe_stream(CAM))
    assert all(isinstance(f, Frame) for f in frames)
    assert _stamps(frames) == _at(*KEY_IDX)
    assert {f.topic for f in frames} == {CAM}


def test_only_keyframes_ever_reach_the_decoder(gop_rec, monkeypatch):
    """The whole point: 16 of the 20 AUs are never reconstructed.

    Asserting the output stamps alone would pass for an implementation that
    decoded everything and filtered afterwards — which is what this replaces.
    """
    calls = []
    real = HevcDecoder.decode
    monkeypatch.setattr(
        HevcDecoder, "decode",
        lambda self, au: (calls.append(au), real(self, au))[1],
    )
    frames = list(Session([gop_rec]).keyframe_stream(CAM))
    assert len(calls) == len(KEY_IDX) == len(frames)


def test_frames_are_identical_to_a_full_decode_of_the_same_stamps(gop_rec):
    """Skipping the P-frames is exact, not an approximation.

    An IRAP access unit carries its own VPS/SPS/PPS, so it reconstructs to the
    same pixels whether or not the GOP around it was decoded.
    """
    sess = Session([gop_rec])
    sparse = {f.t_ns: f.image for f in sess.keyframe_stream(CAM)}
    full = {f.t_ns: f.image for f in Session([gop_rec]).stream([CAM])}
    assert len(sparse) == len(KEY_IDX)  # else the loop below asserts nothing
    assert set(sparse) <= set(full)
    for t_ns, img in sparse.items():
        assert np.array_equal(img, full[t_ns]), f"frame at {t_ns} differs"


def test_every_n_takes_every_nth_keyframe(gop_rec):
    frames = Session([gop_rec]).keyframe_stream(CAM, every_n=2)
    assert _stamps(frames) == _at(0, 10)


def test_the_keyframe_count_runs_across_chunks(rec):
    """A per-file counter would re-phase at the seam; the session-wide one does not.

    Chunk A holds an ODD number of keyframes on purpose — with an even count both
    implementations agree and the test proves nothing.
    """
    a = rec("a.mcap")
    a.add_camera(CAM, indexed_frames(15), keyint=KEYINT)  # keyframes 0, 5, 10
    b = rec("b.mcap")
    b.add_camera(CAM, indexed_frames(20), t0=T0 + 15 * FRAME_DT, keyint=KEYINT)
    frames = Session([a.write(), b.write()]).keyframe_stream(CAM, every_n=2)
    assert _stamps(frames) == _at(0, 10, 20, 30)


def test_fewer_keyframes_than_every_n_still_yields_the_first(gop_rec):
    """A clip that cannot fill one sample period gives one sample, never zero.

    The precondition of the features stage's empty-sidecar guard: it treats "no
    frames at all" as a hard error, so this must not be how a short clip reads.
    """
    assert _stamps(Session([gop_rec]).keyframe_stream(CAM, every_n=10)) == _at(0)


def test_a_lost_keyframe_rephases_the_remainder(rec):
    """Pins the ONE property this design knowingly gives up.

    `every_n` counts keyframes, so losing one shifts every later sample — the cost
    of not bucketing on `t_ns`. Documented in `keyframe_stream` and in
    `docs/alignment.md`; pinned here so nobody silently "fixes" it, and so the
    accepted behaviour is visible rather than merely asserted in prose.
    """
    b = rec()
    b.add_camera(CAM, indexed_frames(20), keyint=KEYINT, drop=(5,))
    frames = Session([b.write()]).keyframe_stream(CAM, every_n=2)
    assert _stamps(frames) == _at(0, 15)  # not (0, 10) — the survivors re-phase


def test_a_second_camera_does_not_share_the_count(rec):
    """The real rig is stereo: one topic's samples must not be thinned by another.

    A `seen` counter shared across topics would halve the effective rate, and a
    topic resolution that matched the wrong eye would return its stamps instead.
    """
    b = rec()
    b.add_camera(CAM, indexed_frames(20), keyint=KEYINT)
    b.add_camera("/ego/camera/1", indexed_frames(20), keyint=KEYINT, skew=1_000_000)
    frames = list(Session([b.write()]).keyframe_stream(CAM, every_n=2))
    assert _stamps(frames) == _at(0, 10)
    assert {f.topic for f in frames} == {CAM}


def test_a_file_without_the_topic_is_skipped(rec):
    """The sidecar shape: `Session(recording, sidecar)` is how stages chain."""
    a = rec("a.mcap")
    a.add_camera(CAM, indexed_frames(20), keyint=KEYINT)
    side = rec("side.mcap")
    side.add_quat("/ego/imu/0/quat", n=4)
    sess = Session([a.write()], [side.write()])
    assert sess.keyframe_cadence(CAM).frames_per_gop == KEYINT
    assert _stamps(sess.keyframe_stream(CAM)) == _at(*KEY_IDX)


def test_the_window_clips_and_anchors_the_phase(gop_rec):
    """`start_ns` is applied to the AU before the count, so it re-phases from there."""
    frames = Session([gop_rec]).keyframe_stream(
        CAM, every_n=2, start_ns=T0 + 5 * FRAME_DT)
    assert _stamps(frames) == _at(5, 15)  # not (10,) — the count starts at 5


def test_end_ns_is_exclusive(gop_rec):
    frames = Session([gop_rec]).keyframe_stream(CAM, end_ns=T0 + 10 * FRAME_DT)
    assert _stamps(frames) == _at(0, 5)


def test_gray_decode_path(gop_rec):
    frames = list(Session([gop_rec]).keyframe_stream(CAM, gray=True))
    assert frames and all(f.is_gray and f.image.ndim == 2 for f in frames)


def test_the_gpu_path_yields_the_same_frames(rec):
    """NVDEC fed a sparse IDR-only bitstream — the one part with no CPU analogue.

    The per-chunk `flush()` is what makes the decoder split safe there (NVDEC holds
    ~9 frames in flight), and the CPU `flush` is a no-op, so this is the only test
    that runs that line at all.

    Its own fixture, larger than the rest of this file's: NVDEC refuses to create a
    decoder below a minimum coded size, and the 96x64 default is under it
    (`cuvidCreateDecoder` error 1).
    """
    pytest.importorskip("cupy")
    pytest.importorskip("PyNvVideoCodec")
    b = rec()
    b.add_camera(CAM, indexed_frames(N, h=192, w=256), keyint=KEYINT)
    path = b.write()
    cpu = {f.t_ns: f.image for f in Session([path]).keyframe_stream(CAM)}
    gpu = list(Session([path]).keyframe_stream(CAM, gpu=True))
    assert [f.t_ns for f in gpu] == sorted(cpu)
    for f in gpu:
        # Device-resident, and within colour-conversion rounding of libav's.
        host = np.asarray(f.image.get())
        assert host.shape == cpu[f.t_ns].shape
        delta = np.abs(host.astype(np.int16) - cpu[f.t_ns].astype(np.int16))
        assert delta.mean() < 2.0, f"frame at {f.t_ns} is not the same picture"


def test_exposure_is_attached_as_on_the_general_path(rec):
    b = rec()
    b.add_camera(CAM, indexed_frames(N), keyint=KEYINT)
    b.add_frame_info(CAM, n=N)
    frames = list(Session([b.write()]).keyframe_stream(CAM))
    assert frames and all(f.exposure is not None for f in frames)
    assert not any(f.exposure.interpolated for f in frames)


def test_a_device_prefixed_recording_resolves_the_canonical_topic(rec):
    b = rec(device="GILABS-AABBCCDD")
    b.add_camera(CAM, indexed_frames(N), keyint=KEYINT)
    frames = list(Session([b.write()]).keyframe_stream(CAM))
    assert _stamps(frames) == _at(*KEY_IDX)
    assert {f.topic for f in frames} == {CAM}


# --- refusals, at the call site ------------------------------------------ #
def test_a_non_video_topic_is_rejected_before_any_read(gop_rec):
    """Not on first `next()`: a mistyped topic must not read as an empty stream."""
    with pytest.raises(ValueError) as exc:
        Session([gop_rec]).keyframe_stream("/ego/camera/9")
    # Naming what IS there is the whole point — a bare refusal leaves the caller
    # guessing at the very typo it caught.
    assert "is not a foxglove.CompressedVideo topic" in str(exc.value)
    assert f"video topics present: {CAM}" in str(exc.value)


def test_a_recording_with_no_video_at_all_says_so(rec):
    b = rec()
    b.add_quat("/ego/imu/0/quat", n=4)
    with pytest.raises(ValueError, match="video topics present: none"):
        Session([b.write()]).keyframe_stream(CAM)


def test_a_stream_with_no_keyframes_fails_rather_than_yielding_nothing(
    gop_rec, monkeypatch
):
    """`is_keyframe`'s quiet `False` is only safe because this backstop is loud."""
    monkeypatch.setattr(session_mod, "is_keyframe", lambda fmt, au: False)
    with pytest.raises(ValueError, match="no keyframe among them"):
        list(Session([gop_rec]).keyframe_stream(CAM))


def test_every_n_below_one_is_rejected(gop_rec):
    with pytest.raises(ValueError, match="every_n must be >= 1"):
        Session([gop_rec]).keyframe_stream(CAM, every_n=0)


def test_gray_and_gpu_together_are_rejected(gop_rec):
    with pytest.raises(ValueError, match="decodes RGB only"):
        Session([gop_rec]).keyframe_stream(CAM, gray=True, gpu=True)
