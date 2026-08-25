"""Round trip through the on-disk form, because both halves live here.

The producer (a conversion) and the consumer (training) go through
this one module precisely so they cannot disagree about the layout — so what is
worth pinning is that what `write_episode` puts down is what `episodes()` hands
back, channel for channel.
"""

import json

import numpy as np
import pytest

from visio_schema.dataset import (
    COLLAR,
    POSE_DIM,
    DatasetError,
    DatasetSpec,
    LeRobotDataset,
    ee_gripper_width,
    ee_pose,
    features,
    image,
    spec_from_info,
)

N = 12
FPS = 30.0
SHAPES = {"head": (3, 480, 640)}


def spec() -> DatasetSpec:
    return DatasetSpec(
        ee_names=("left", "right"), image_slots=("head",), grid_slot="head"
    )


def channels(seed: int = 0) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)

    def pose(s):
        quat = np.random.default_rng(s).normal(size=(N, 4))
        quat /= np.linalg.norm(quat, axis=1, keepdims=True)
        return np.concatenate(
            [rng.uniform(-1, 1, (N, 3)), quat], axis=1
        ).astype(np.float32)

    ch = {"observation.collar.pose": pose(seed)}
    for i, ee in enumerate(("left", "right")):
        ch[f"observation.{ee}.pose"] = pose(seed + 1 + i)
        ch[f"action.{ee}.pose"] = pose(seed + 3 + i)
        ch[f"observation.{ee}.gripper_width"] = rng.uniform(
            0, 0.085, (N, 1)
        ).astype(np.float32)
        ch[f"action.{ee}.gripper_width"] = rng.uniform(
            0, 0.085, (N, 1)
        ).astype(np.float32)
    return ch


def build(tmp_path, n_episodes: int = 2) -> LeRobotDataset:
    ds = LeRobotDataset(tmp_path / "out")
    lengths, start = {}, 0
    for i in range(n_episodes):
        lengths[i] = ds.write_episode(
            i, channels(seed=i * 10), fps=FPS, global_start=start
        )
        start += lengths[i]
    ds.write_meta(
        spec(), fps=FPS, video_shapes=SHAPES,
        episode_lengths=lengths, tasks=["pick it up"],
    )
    return ds


# ---- round trip ------------------------------------------------------ #


def test_channels_survive_the_round_trip(tmp_path):
    ds = build(tmp_path, n_episodes=1)
    written = channels(seed=0)
    got = ds.episode(0).channels
    assert set(got) == set(written)
    for name, arr in written.items():
        np.testing.assert_allclose(got[name], arr, atol=1e-7)


def test_pose_channels_keep_their_width(tmp_path):
    ep = build(tmp_path, 1).episode(0)
    assert ep.channels["observation.left.pose"].shape == (N, 7)
    assert ep.channels["observation.left.gripper_width"].shape == (N, 1)
    assert ep.length == N


def test_episodes_are_yielded_in_order(tmp_path):
    ds = build(tmp_path, n_episodes=3)
    assert [e.index for e in ds.episodes()] == [0, 1, 2]


def test_the_written_spec_reads_back(tmp_path):
    ds = build(tmp_path, 1)
    assert ds.spec.ee_names == ("left", "right")
    assert ds.spec.grid_slot == "head"


# ---- the global index ------------------------------------------------ #


def test_index_is_global_across_episodes(tmp_path):
    """LeRobot's `index` addresses the whole dataset. A writer that restarted
    it per episode would make rows non-unique."""
    import pyarrow.parquet as pq

    ds = build(tmp_path, n_episodes=2)
    second = pq.read_table(ds.data_path(1))
    idx = np.asarray(second["index"].to_pylist())
    np.testing.assert_array_equal(idx, np.arange(N, 2 * N))
    np.testing.assert_array_equal(
        np.asarray(second["frame_index"].to_pylist()), np.arange(N)
    )


def test_timestamp_is_the_fps_grid(tmp_path):
    import pyarrow.parquet as pq

    ds = build(tmp_path, 1)
    ts = np.asarray(pq.read_table(ds.data_path(0))["timestamp"].to_pylist())
    np.testing.assert_allclose(ts, np.arange(N) / FPS, atol=1e-6)


# ---- meta ------------------------------------------------------------ #


def test_meta_carries_the_stamp_and_features(tmp_path):
    info = json.loads((build(tmp_path, 2).meta_dir / "info.json").read_text())
    assert info["visio_dataset_version"] == 1
    assert info["ee_names"] == ["left", "right"]
    assert info["total_episodes"] == 2 and info["total_frames"] == 2 * N
    assert info["features"]["observation.left.pose"]["shape"] == [7]


def test_provenance_is_written_but_is_not_a_channel(tmp_path):
    """Recorded for audit and merge admission — never for the dataloader."""
    ds = LeRobotDataset(tmp_path / "p")
    lengths = {0: ds.write_episode(0, channels(), fps=FPS, global_start=0)}
    ds.write_meta(
        spec(), fps=FPS, video_shapes=SHAPES, episode_lengths=lengths,
        tasks=["t"], provenance={"source": "umi_rig_v1", "z_down": 0.048},
    )
    info = json.loads((ds.meta_dir / "info.json").read_text())
    assert info["provenance"]["z_down"] == 0.048
    assert "provenance" not in ds.episode(0).channels


# ---- failures name the fault ----------------------------------------- #


def test_a_missing_channel_is_refused_at_write(tmp_path):
    ch = channels()
    del ch["action.right.pose"]
    with pytest.raises(DatasetError, match=r"action\.right\.pose"):
        LeRobotDataset(tmp_path / "x").write_episode(
            0, ch, fps=FPS, global_start=0
        )


def test_ragged_channels_are_refused_at_write(tmp_path):
    ch = channels()
    ch["observation.left.pose"] = ch["observation.left.pose"][:-1]
    with pytest.raises(DatasetError, match="rows, expected"):
        LeRobotDataset(tmp_path / "x").write_episode(
            0, ch, fps=FPS, global_start=0
        )


def test_a_root_without_info_says_so(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(DatasetError, match="not a dataset root"):
        LeRobotDataset(tmp_path / "empty").load_info()


def test_an_episode_file_with_no_rows_is_refused(tmp_path):
    """An interrupted write. Skipping it — what this did — hides it from the
    gate, which reads through `episodes()` precisely to catch a malformed
    dataset: the frame-count median shifts and nothing compares the episodes
    found against info.json's total."""
    import pyarrow.parquet as pq

    ds = build(tmp_path, 1)
    table = pq.read_table(ds.data_path(0))
    pq.write_table(table.slice(0, 0), ds.data_path(1))
    with pytest.raises(DatasetError, match="carries no rows"):
        list(ds.episodes())


def test_an_episode_is_read_from_its_own_file(tmp_path):
    """`data_path` is deterministic, so reading episode N must not decode
    episodes 0..N-1 first — the serving entries address episodes by index."""
    ds = build(tmp_path, 4)
    scanned: list[int] = []
    original = ds.data_files
    ds.data_files = lambda: (scanned.append(1), original())[1]
    assert ds.episode(3).index == 3
    assert not scanned, "episode() fell back to scanning every file"


# ---- the v2.1 meta encoding of the spec ------------------------------- #

V21_SHAPES = {"head": (3, 480, 640), "left_wrist": (3, 480, 640)}
V21_SPEC = DatasetSpec(
    ee_names=("left", "right"),
    image_slots=("head", "left_wrist"),
    grid_slot="head",
)


def _info_of(s: DatasetSpec) -> dict:
    return {**s.stamp(), "features": features(s, V21_SHAPES)}


# ---- the v2.1 meta encoding ----------------------------------------- #


def test_features_carry_widths_names_and_bookkeeping():
    f = features(V21_SPEC, V21_SHAPES)
    assert f[COLLAR]["shape"] == [POSE_DIM]
    assert f[COLLAR]["names"][:3] == ["x", "y", "z"]
    assert f[COLLAR]["names"][-1] == "qw"          # xyzw, w last
    assert f[ee_gripper_width("left")]["shape"] == [1]
    assert f[ee_gripper_width("left")]["names"] == ["width_m"]   # METRES
    assert f[image("head")]["dtype"] == "video"
    assert f["timestamp"]["dtype"] == "float32"
    assert f["episode_index"]["dtype"] == "int64"


def test_features_refuses_a_slot_with_no_encoded_shape():
    with pytest.raises(DatasetError, match="left_wrist"):
        features(V21_SPEC, {"head": (3, 480, 640)})



def test_round_trips_through_the_v21_info_json():
    s = V21_SPEC
    back = spec_from_info(_info_of(s))
    assert back.ee_names == s.ee_names
    assert back.grid_slot == s.grid_slot
    assert set(back.image_slots) == set(s.image_slots)


def test_truncated_meta_names_the_missing_features_block():
    """Defaulting `features` to {} would surface as "grid_slot is not among
    image_slots []" — a camera-config fault for a truncated file."""
    with pytest.raises(DatasetError, match="no 'features'"):
        spec_from_info(V21_SPEC.stamp())


def test_missing_channel_names_the_channel():
    s = V21_SPEC
    info = _info_of(s)
    info["features"].pop(ee_pose("right"))
    with pytest.raises(DatasetError, match=ee_pose("right")):
        spec_from_info(info)


def test_wrong_width_is_caught():
    s = V21_SPEC
    info = _info_of(s)
    info["features"][COLLAR]["shape"] = [6]     # rotvec, not quat
    with pytest.raises(DatasetError, match="expected \\[7\\]"):
        spec_from_info(info)


def test_unknown_channels_are_tolerated():
    """Additive-only versioning is worth nothing if readers refuse additions."""
    s = V21_SPEC
    info = _info_of(s)
    info["features"]["observation.depth.head"] = {
        "dtype": "video", "shape": [1, 480, 640], "names": None
    }
    assert spec_from_info(info).ee_names == s.ee_names


# ---- meta/episodes.jsonl --------------------------------------------- #


def test_episode_lengths_read_back(tmp_path):
    ds = build(tmp_path, n_episodes=3)
    assert ds.load_episode_lengths() == {0: N, 1: N, 2: N}


def test_missing_episodes_jsonl_says_write_meta_was_never_run(tmp_path):
    ds = build(tmp_path)
    (ds.meta_dir / "episodes.jsonl").unlink()
    with pytest.raises(DatasetError, match="write_meta was never run"):
        ds.load_episode_lengths()


def test_a_duplicate_episode_row_is_refused(tmp_path):
    """Two rows for one episode make the frame index ambiguous, and the
    duplicate would silently win."""
    ds = build(tmp_path)
    path = ds.meta_dir / "episodes.jsonl"
    path.write_text(path.read_text() + json.dumps(
        {"episode_index": 0, "length": 99, "tasks": ["x"]}) + "\n")
    with pytest.raises(DatasetError, match="listed twice"):
        ds.load_episode_lengths()


def test_an_empty_episodes_jsonl_is_refused(tmp_path):
    ds = build(tmp_path)
    (ds.meta_dir / "episodes.jsonl").write_text("")
    with pytest.raises(DatasetError, match="is empty"):
        ds.load_episode_lengths()


def test_several_episodes_in_one_parquet_are_refused(tmp_path):
    """One episode per file is what the writer produces and what `video_path`
    assumes; several in one file would read fine and then point at an mp4 that
    does not exist."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    ds = build(tmp_path)
    path = ds.data_path(0)
    table = pq.read_table(path)
    idx = np.asarray(table["episode_index"]).copy()
    idx[N // 2:] = 7
    table = table.set_column(
        table.column_names.index("episode_index"), "episode_index",
        pa.array(idx),
    )
    pq.write_table(table, path)
    with pytest.raises(DatasetError, match="expected exactly one episode"):
        list(ds.episodes())


def test_an_episode_whose_column_disagrees_with_its_name_is_refused(tmp_path):
    ds = build(tmp_path)
    path = ds.data_path(1)
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    table = table.set_column(
        table.column_names.index("episode_index"), "episode_index",
        pa.array(np.full(len(table), 4, dtype=np.int64)),
    )
    pq.write_table(table, path)
    with pytest.raises(DatasetError, match="file name and the column disagree"):
        ds.episode(1)
