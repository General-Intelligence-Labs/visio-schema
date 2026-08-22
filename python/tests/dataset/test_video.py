"""`Mp4Writer` — the dataset video encoder.

The decode half moved to `visio_schema.reader` (`Session.stream` yields decoded
`Frame`s), and its tests with it. What was pinned here and is NOT ported: the
byte-signature codec table, which described a sniffing step the reader replaces
by reading the wire `format` field, and two closed-decoder edge cases belonging
to a class that no longer exists.

What IS ported lives with the behaviour it guards: the frames-in == frames-out
invariant is `visio_schema`'s decoder test, and the nearest/hold-last sampling
semantics are `sync`'s.
"""

from fractions import Fraction

import numpy as np

from visio_schema.dataset.video import Mp4Writer


def test_encode_params_all_intra_no_bframes(tmp_path, solid_frames):
    """gop=1 => every frame a keyframe: all-intra random access, which is what a
    training loader seeking to an arbitrary index depends on."""
    import av

    from visio_schema.dataset.video import Mp4Writer, VideoEncodeParams

    frames = solid_frames(6)
    path = tmp_path / "intra.mp4"
    params = VideoEncodeParams(
        encoder="libx264", gop_size=1, quality=21, no_bframes=True
    )
    with Mp4Writer(path, 30, params=params) as writer:
        for frame in frames:
            writer.write(frame)
    assert writer.shape == (3, 48, 64)
    with av.open(str(path)) as container:
        decoded = list(container.decode(video=0))
        assert len(decoded) == len(frames)
        assert all(f.key_frame for f in decoded)


def test_writer_keeps_no_reference_to_the_frames_it_wrote(tmp_path):
    """The property the class exists for. Collecting an episode's decoded frames
    costs ~6.2 MB each per camera; written incrementally, a frame is encodable
    and then garbage.

    Pinned with a weakref rather than by watching a generator, which only ever
    proved the TEST's own loop pulled one at a time — an `Mp4Writer` that
    appended everything to a list and encoded in `close()` passed that.
    """
    import gc
    import weakref

    import numpy as np

    from visio_schema.dataset.video import Mp4Writer

    first = np.full((48, 64, 3), 40, np.uint8)
    seen = weakref.ref(first)
    with Mp4Writer(tmp_path / "streamed.mp4", 30) as writer:
        writer.write(first)
        del first
        gc.collect()
        assert seen() is None, "the writer is still holding frame 0"
        for i in range(1, 8):
            writer.write(np.full((48, 64, 3), 40 + i * 20, np.uint8))
    assert writer.shape == (3, 48, 64)


def test_writer_downscales_to_the_long_side(tmp_path, solid_frames):
    """`long_side` caps the longer edge so the stored video matches what
    inference downscales to; both dims stay even for yuv420p. Asserted on the
    FILE, not just the reported shape — a writer that returns the right tuple
    while muxing full-size frames would otherwise pass."""
    import av

    from visio_schema.dataset.video import Mp4Writer

    path = tmp_path / "small.mp4"
    with Mp4Writer(path, 30, long_side=32) as writer:
        for frame in solid_frames(3):  # 48x64
            writer.write(frame)
    assert writer.shape == (3, 24, 32)
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        assert (stream.width, stream.height) == (32, 24)


def test_writer_reports_no_shape_when_nothing_was_written(tmp_path):
    """A dataset's info.json references the file by shape; a zero-frame writer
    has none to give, and must not be asked to invent one."""
    import pytest

    from visio_schema.dataset.video import Mp4Writer

    path = tmp_path / "empty.mp4"
    with Mp4Writer(path, 30) as writer:
        pass
    assert not path.exists()
    with pytest.raises(ValueError, match="no frames were written"):
        _ = writer.shape


def test_an_exception_after_frames_are_written_drops_the_partial_file(tmp_path):
    """Closing alone writes the trailer, leaving a VALID but short mp4 that a
    dataset would reference at a row count it does not match. The file must go,
    and the caller's exception must be the one that propagates."""
    import numpy as np
    import pytest

    from visio_schema.dataset.video import Mp4Writer

    path = tmp_path / "partial.mp4"
    with pytest.raises(RuntimeError, match="boom"):
        with Mp4Writer(path, 30) as writer:
            for _ in range(3):
                writer.write(np.zeros((48, 64, 3), np.uint8))
            raise RuntimeError("boom")
    assert not path.exists()


def test_named_encoder_must_exist(tmp_path):
    import av
    import numpy as np
    import pytest

    from visio_schema.dataset.video import Mp4Writer, VideoEncodeParams

    # av raises UnknownCodecError (a ValueError) for a bogus encoder name
    with pytest.raises((ValueError, av.error.FFmpegError)):
        with Mp4Writer(
            tmp_path / "x.mp4",
            30,
            params=VideoEncodeParams(encoder="not_a_real_encoder"),
        ) as writer:
            writer.write(np.zeros((48, 64, 3), dtype=np.uint8))


def test_a_fractional_frame_rate_reaches_the_encoder_as_a_ratio(tmp_path):
    """The grid rate is a float, and PyAV wants a rational. 29.97 fps is
    30000/1001 exactly; rounding it to 30 makes a stored video drift a frame
    every ~33 s against the parquet timestamps its rows are addressed by."""
    import av

    frames = [np.zeros((32, 48, 3), dtype=np.uint8) for _ in range(3)]
    path = tmp_path / "ntsc.mp4"
    with Mp4Writer(path, 30000 / 1001) as writer:
        for frame in frames:
            writer.write(frame)
    with av.open(str(path)) as container:
        assert container.streams.video[0].average_rate == Fraction(30000, 1001)


def test_an_integral_frame_rate_survives_as_an_integer(tmp_path):
    import av

    path = tmp_path / "thirty.mp4"
    with Mp4Writer(path, 30.0) as writer:
        writer.write(np.zeros((32, 48, 3), dtype=np.uint8))
    with av.open(str(path)) as container:
        assert container.streams.video[0].average_rate == Fraction(30, 1)


# ---- the shared downscale rule ---------------------------------------- #


def test_scaled_dims_caps_the_long_side_preserving_aspect():
    from visio_schema.dataset import scaled_dims

    assert scaled_dims(1920, 1080, 480) == (480, 270)
    assert scaled_dims(1080, 1920, 480) == (270, 480)      # portrait
    assert scaled_dims(640, 480, None) == (640, 480)       # no cap
    assert scaled_dims(320, 240, 480) == (320, 240)        # already small


def test_scaled_dims_forces_even_dims():
    """yuv420p subsamples chroma 2x2, so an odd dimension is not encodable."""
    from visio_schema.dataset import scaled_dims

    for w, h in ((1000, 563), (999, 501), (3, 3)):
        ow, oh = scaled_dims(w, h, 500)
        assert ow % 2 == 0 and oh % 2 == 0
        assert ow >= 2 and oh >= 2


def test_scaled_dims_needs_neither_av_nor_pyarrow():
    """It is imported by live pipelines that encode no video and read no
    parquet; pulling either onto that path is a real cost (measured ~50 MB)."""
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; from visio_schema.dataset import scaled_dims; "
         "print('av' in sys.modules, 'pyarrow' in sys.modules)"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "False False"


def test_the_default_encoder_does_not_depend_on_the_host():
    """A hardware encoder produces different pixels. Auto-probing by default
    made a dataset's bytes a property of the machine that converted it, which
    is invisible until two datasets that should match do not."""
    from visio_schema.dataset import VideoEncodeParams

    assert VideoEncodeParams().encoder == "libx264"
