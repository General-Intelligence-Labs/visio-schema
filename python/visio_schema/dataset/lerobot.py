"""The spec as a LeRobot v2.1 directory — reading and writing, both ways.

One module owns both directions so a producer and a consumer of the same
dataset cannot disagree about the layout. Other on-disk formats may implement
the same `visio_schema.dataset.spec`; this one is LeRobot v2.1: per-episode
parquet under ``data/``, per-slot mp4 under ``videos/``, json(l) meta.

An episode is handed over as `spec.Episode` — ``{channel -> (N, D) float32}``
— so nothing downstream has to know the column names or the storage.

Videos are NOT written by `LeRobotDataset`. Encoding belongs to whatever owns
the frames (see `visio_schema.dataset.video`); the writer takes the resulting
shapes in `write_meta`. Pure pyarrow + json; no lerobot package dependency.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from visio_schema.dataset.spec import (
    GRIPPER_NAMES,
    POSE_DIM,
    POSE_NAMES,
    DatasetError,
    DatasetSpec,
    Episode,
    ee_names_of,
    image,
    slot_of,
)

DATA_PATH = "data/chunk-{chunk:03d}/file-{index:06d}.parquet"
VIDEO_PATH = "videos/{slot}/chunk-{chunk:03d}/file-{index:06d}.mp4"
CHUNK_SIZE = 1000
INFO = "info.json"
EPISODES = "episodes.jsonl"
TASKS = "tasks.jsonl"

# LeRobot v2.1's own bookkeeping columns, carried unchanged.
BOOKKEEPING: dict[str, tuple[str, int]] = {
    "timestamp": ("float32", 1),
    "index": ("int64", 1),
    "episode_index": ("int64", 1),
    "frame_index": ("int64", 1),
    "task_index": ("int64", 1),
}

_EPISODE_INDEX = "episode_index"


class LeRobotDataset:
    """A LeRobot-v2.1-layout dataset rooted at ``root``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # ---- layout ------------------------------------------------------- #

    @property
    def meta_dir(self) -> Path:
        return self.root / "meta"

    def data_path(self, index: int) -> Path:
        return self.root / DATA_PATH.format(
            chunk=index // CHUNK_SIZE, index=index
        )

    def video_path(self, slot: str, index: int) -> Path:
        return self.root / VIDEO_PATH.format(
            slot=slot, chunk=index // CHUNK_SIZE, index=index
        )

    def data_files(self) -> list[Path]:
        files = sorted((self.root / "data").glob("chunk-*/*.parquet"))
        if not files:
            raise DatasetError(f"no parquet under {self.root / 'data'}")
        return files

    # ---- reading ------------------------------------------------------ #

    def load_info(self) -> dict[str, Any]:
        path = self.meta_dir / INFO
        if not path.exists():
            raise DatasetError(
                f"{self.root}: no meta/{INFO} — not a dataset root"
            )
        return json.loads(path.read_text())

    @cached_property
    def spec(self) -> DatasetSpec:
        """The dataset's own spec, validated. Cached: every episode read would
        otherwise re-parse and re-check the same info.json."""
        return spec_from_info(self.load_info())

    def episodes(self) -> Iterator[Episode]:
        """Every episode, in file then index order."""
        spec = self.spec
        for path in self.data_files():
            table = pq.read_table(path)
            if len(table) == 0:
                # Not skipped: an episode file with no rows is an interrupted
                # write, and the gate that exists to catch a malformed dataset
                # reads through here — so skipping would hide it from the one
                # check looking for it.
                raise DatasetError(f"{path}: parquet carries no rows")
            episode_ids = np.unique(
                _column(table, _EPISODE_INDEX).astype(np.int64).reshape(-1)
            )
            if len(episode_ids) != 1:
                # One episode per file is what `write_episode` produces and what
                # `video_path` assumes. Several in one parquet would read fine
                # here and then point at an mp4 that does not exist.
                raise DatasetError(
                    f"{path}: carries episodes {episode_ids.tolist()} — "
                    "expected exactly one episode per file"
                )
            yield Episode(int(episode_ids[0]), _channels(table, spec))

    def episode(self, index: int) -> Episode:
        """One episode, read from its own file.

        `data_path` is deterministic and one episode is one parquet, so this
        opens exactly that file rather than scanning `episodes()` for a match,
        which would decode every earlier episode first. A file whose
        `episode_index` disagrees with its name is refused rather than
        searched for elsewhere — that disagreement desyncs the videos, which
        are addressed by index alone.
        """
        path = self.data_path(index)
        table = pq.read_table(path)
        found = np.unique(
            _column(table, _EPISODE_INDEX).astype(np.int64).reshape(-1)
        )
        if found.tolist() != [index]:
            raise DatasetError(
                f"{path}: carries episode(s) {found.tolist()}, expected "
                f"[{index}] — the file name and the column disagree"
            )
        return Episode(index, _channels(table, self.spec))

    def load_episode_lengths(self) -> dict[int, int]:
        """``meta/episodes.jsonl`` as ``{episode_index: length}`` (fail loud).

        The jsonl is the only per-episode index the meta carries; readers that
        need a global frame->episode mapping build it from here and
        cross-check against the parquet row counts rather than trusting
        either alone.
        """
        path = self.meta_dir / EPISODES
        if not path.exists():
            raise DatasetError(
                f"{self.root}: no meta/{EPISODES} — write_meta was never run"
            )
        out: dict[int, int] = {}
        for line in path.read_text().splitlines():
            row = json.loads(line)
            index = int(row["episode_index"])
            if index in out:
                raise DatasetError(
                    f"meta/{EPISODES}: episode {index} listed twice"
                )
            out[index] = int(row["length"])
        if not out:
            raise DatasetError(f"meta/{EPISODES} is empty")
        return out

    # ---- writing ------------------------------------------------------ #

    def write_episode(
        self,
        index: int,
        channels: dict[str, np.ndarray],
        *,
        fps: float,
        global_start: int,
        task_index: int = 0,
    ) -> int:
        """Write one episode's parquet; returns its frame count.

        ``global_start`` is the running frame total across episodes written so
        far — LeRobot's ``index`` column is global, not per-episode, and a
        writer that restarted it at 0 would produce a dataset whose rows cannot
        be addressed uniquely.
        """
        spec = _spec_of(channels)
        missing = [c for c in spec.table_channels if c not in channels]
        if missing:
            raise DatasetError(
                f"episode {index}: missing channel(s) {missing}"
            )
        n = len(next(iter(channels.values())))
        for name, arr in channels.items():
            if len(arr) != n:
                raise DatasetError(
                    f"episode {index}: {name} has {len(arr)} rows, expected {n}"
                )
        arrays = {
            name: _list_column(np.asarray(arr, dtype=np.float32))
            for name, arr in channels.items()
        }
        arrays["timestamp"] = pa.array(
            (np.arange(n, dtype=np.float64) / float(fps)).astype(np.float32)
        )
        arrays["index"] = pa.array(
            np.arange(global_start, global_start + n, dtype=np.int64)
        )
        arrays[_EPISODE_INDEX] = pa.array(np.full(n, index, dtype=np.int64))
        arrays["frame_index"] = pa.array(np.arange(n, dtype=np.int64))
        arrays["task_index"] = pa.array(np.full(n, task_index, dtype=np.int64))

        out = self.data_path(index)
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(arrays), out)
        return n

    def write_meta(
        self,
        spec: DatasetSpec,
        *,
        fps: float,
        video_shapes: dict[str, tuple[int, int, int]],
        episode_lengths: dict[int, int],
        tasks: list[str],
        provenance: dict[str, Any] | None = None,
    ) -> None:
        """Write ``meta/`` once, after every episode's parquet exists.

        ``provenance`` is the producer's record of HOW the data was made —
        source id, registration, reconstruction parameters. It is written so a
        dataset can be audited and so a merge can refuse to mix incompatible
        conversions; nothing downstream of here reads it. A dataloader that did
        would be branching on where the data came from — exactly what a fixed
        spec exists to prevent.
        """
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        info: dict[str, Any] = {
            **spec.stamp(),
            "fps": fps,
            "total_episodes": len(episode_lengths),
            "total_frames": sum(episode_lengths.values()),
            "data_path": DATA_PATH,
            "video_path": VIDEO_PATH,
            "chunks_size": CHUNK_SIZE,
            "features": features(spec, video_shapes),
        }
        if provenance:
            info["provenance"] = provenance
        (self.meta_dir / INFO).write_text(json.dumps(info, indent=2) + "\n")
        _write_jsonl(
            self.meta_dir / EPISODES,
            [
                {"episode_index": i, "length": episode_lengths[i],
                 "tasks": tasks}
                for i in sorted(episode_lengths)
            ],
        )
        _write_jsonl(
            self.meta_dir / TASKS,
            [{"task_index": i, "task": t} for i, t in enumerate(tasks)],
        )


# ---- the v2.1 meta encoding of the spec ------------------------------- #


def features(
    spec: DatasetSpec, video_shapes: dict[str, tuple[int, int, int]]
) -> dict[str, dict[str, Any]]:
    """The v2.1 ``features`` dict for ``spec``.

    ``video_shapes`` maps camera slot -> the encoded ``(C, H, W)``; the writer
    knows it only after encoding, which is why it is a parameter.
    """
    missing = sorted(set(spec.image_slots) - set(video_shapes))
    if missing:
        raise DatasetError(
            f"no encoded shape for camera slot(s) {missing}; "
            f"got {sorted(video_shapes)}"
        )
    out: dict[str, dict[str, Any]] = {}
    for name, (dtype, dim) in BOOKKEEPING.items():
        out[name] = {"dtype": dtype, "shape": [dim], "names": None}
    for channel in spec.pose_channels:
        out[channel] = {
            "dtype": "float32",
            "shape": [POSE_DIM],
            "names": list(POSE_NAMES),
        }
    for channel in spec.scalar_channels:
        out[channel] = {
            "dtype": "float32",
            "shape": [1],
            "names": list(GRIPPER_NAMES),
        }
    for slot in spec.image_slots:
        out[image(slot)] = {
            "dtype": "video",
            "shape": list(video_shapes[slot]),
            "names": ["channels", "height", "width"],
        }
    return out


def spec_from_info(info: dict[str, Any]) -> DatasetSpec:
    """Rebuild the spec from a loaded v2.1 ``info.json`` (fail loud).

    The identity keys and the version rule are the spec's
    (`DatasetSpec.from_stamp`); what this adds is the v2.1 part — where the
    camera slots and the channel WIDTHS are written down.
    """
    declared = info.get("features")
    if not declared:
        raise DatasetError(
            "info.json carries no 'features' — the meta is truncated or was "
            "written by something that is not a dataset writer"
        )
    slots = tuple(
        slot for slot in map(slot_of, declared) if slot is not None
    )
    spec = DatasetSpec.from_stamp(info, slots)
    spec.verify_channels(declared)
    verify_widths(spec, declared)
    return spec


def verify_widths(spec: DatasetSpec, declared: dict[str, Any]) -> None:
    """Fail loud unless every channel is stored at the width the spec implies.

    A width is a STORAGE fact — the spec says a pose has 7 components, this
    says the file agrees.
    """
    expected = {
        **{c: POSE_DIM for c in spec.pose_channels},
        **{c: 1 for c in spec.scalar_channels},
    }
    wrong = [
        f"{channel} has shape {declared[channel].get('shape')}, "
        f"expected [{width}]"
        for channel, width in expected.items()
        if declared[channel].get("shape") != [width]
    ]
    if wrong:
        raise DatasetError("channel width mismatch: " + "; ".join(wrong))


# ---- helpers ---------------------------------------------------------- #


def _list_column(values: np.ndarray) -> pa.Array:
    """An ``(N, D)`` float32 array as one arrow list column, without boxing.

    The obvious ``pa.array(values.tolist(), type=pa.list_(pa.float32()))``
    builds N*D Python floats and N lists on the way through — measured 261x
    slower than this on a 3600-row episode, all of it in the parent process
    where it serialises against draining the worker pool.
    """
    flat = pa.array(np.ascontiguousarray(values).reshape(-1))
    return pa.FixedSizeListArray.from_arrays(flat, values.shape[1])


def _column(table: pa.Table, name: str) -> np.ndarray:
    """One column as a numpy array, list-typed or flat, without boxing.

    The obvious ``to_pylist()`` materialises every value as a Python object
    and every row as a Python list, then makes numpy re-probe each row's
    length: measured 713x slower over an episode, and a whole-dataset consistency check reads
    every episode.
    """
    if name not in table.column_names:
        raise DatasetError(
            f"parquet is missing required column {name!r}; "
            f"has {table.column_names}"
        )
    column = table[name].combine_chunks()
    if not pa.types.is_list(column.type) and not pa.types.is_fixed_size_list(
        column.type
    ):
        return column.to_numpy(zero_copy_only=False)
    values = column.flatten().to_numpy(zero_copy_only=False)
    return values.reshape(len(column), -1)


def _channels(table: pa.Table, spec: DatasetSpec) -> dict[str, np.ndarray]:
    """The spec's pose + scalar channels as ``(N, D)`` float32 arrays."""
    out: dict[str, np.ndarray] = {}
    for name in spec.table_channels:
        arr = _column(table, name).astype(np.float32)
        out[name] = arr.reshape(len(arr), -1)
    return out


def _spec_of(channels: dict[str, np.ndarray]) -> DatasetSpec:
    """Infer the arity a channel dict implies, so `write_episode` can check it
    is complete rather than writing a dataset missing an arm.

    First-seen order, via the shared inverse — NOT sorted. A sorted copy here
    would disagree with the descriptor's declaration order (and so with the
    ``ee_names`` the dataset stamps) for any source that declares its arms in
    another order.
    """
    ees = ee_names_of(channels)
    if not ees:
        raise DatasetError(
            "no observation.<ee>.pose channels — cannot tell what this episode "
            f"carries; got {sorted(channels)}"
        )
    # image_slots/grid_slot are irrelevant to the parquet; a placeholder keeps
    # the completeness check honest without inventing camera facts.
    return DatasetSpec(
        ee_names=ees, image_slots=("_",), grid_slot="_"
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
