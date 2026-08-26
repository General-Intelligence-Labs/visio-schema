"""The dataset spec: which channels an episode dataset carries, and how.

This is a **fixed schema, not a configurable descriptor** — a contract with
knobs is not a contract. What varies is the *arity*: how many end-effectors and
cameras a dataset carries, each named by its entry.

The spec is storage-agnostic. `visio_schema.dataset.lerobot` implements it as a
LeRobot v2.1 directory; other on-disk formats may implement the same spec.

## The channels

One row per reference-camera frame. Every pose is expressed **independently in
the world frame**, including the collar — nothing is stored pre-divided by
anything else.

  observation.collar.pose            the frame this episode's motion references
  observation.<ee>.pose              measured end-effector
  observation.<ee>.gripper_width     measured, METRES
  action.<ee>.pose                   commanded end-effector
  action.<ee>.gripper_width          commanded, METRES
  observation.images.<slot>          one per camera

## Why world frame, including the collar

Storing poses base-relative would discard the collar trajectory; storing
everything in world derives it. And because the collar is just another channel,
a session-arbitrary world yaw is **common mode** — any collar-relative
derivation downstream cancels it, so the spec needs no canonical yaw at all.

A stationary rig writes a constant collar; a mobile base writes a varying one.
That difference is *data*, not a config flag, which is what keeps one schema.

## Conventions

Units are metres and seconds. Rotations are quaternions ``[qx, qy, qz, qw]``,
canonicalised to ``w >= 0``. World ``+Z`` is up (gravity-aligned). Gripper width
is the absolute jaw opening in metres — **not** normalised. Normalisation and
any change of rotation encoding are *modelling* choices, applied downstream, so
the stored data stays faithful to what was measured.

Quaternions rather than rotation vectors for the same reason: the SO(3) log map
wraps near pi, which is a hazard in stored data. A consumer that wants a rotvec
takes the log; the dataset does not.

## Versioning

`DATASET_VERSION` is a single integer and channel additions are **additive
only**, so admission is one comparison rather than a field-by-field walk.
A reader refuses a dataset newer than itself: additive changes are readable in
principle, but this reader cannot verify that an addition it does not know about
leaves the channels it does know unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np

DATASET_VERSION = 1

# Dataset-meta keys that identify the spec (any storage format writes them).
VERSION_KEY = "visio_dataset_version"
EE_NAMES_KEY = "ee_names"
GRID_SLOT_KEY = "grid_slot"
FPS_KEY = "fps"

POSE_DIM = 7
POSE_NAMES = ("x", "y", "z", "qx", "qy", "qz", "qw")
GRIPPER_NAMES = ("width_m",)

COLLAR = "observation.collar.pose"

def ee_pose(ee: str) -> str:
    """Measured end-effector pose channel for ``ee``."""
    return f"observation.{ee}.pose"


def ee_gripper_width(ee: str) -> str:
    """Measured gripper width channel for ``ee`` (metres)."""
    return f"observation.{ee}.gripper_width"


def action_ee_pose(ee: str) -> str:
    """Commanded end-effector pose channel for ``ee``."""
    return f"action.{ee}.pose"


def action_ee_gripper_width(ee: str) -> str:
    """Commanded gripper width channel for ``ee`` (metres)."""
    return f"action.{ee}.gripper_width"


def image(slot: str) -> str:
    """Video channel for camera ``slot``."""
    return f"observation.images.{slot}"


def ee_of(channel: str) -> str | None:
    """The end-effector a channel belongs to, or None if it names no arm.

    The inverse of the four constructors above, and the reason it exists: every
    consumer that wanted it was splitting on ``"."`` and re-deriving the
    convention — several call sites, of which some disagreed about whether
    the result was ordered or sorted.

    Returns None for the collar, the images and anything unrecognised, so a
    caller filters rather than branching on a raised exception.
    """
    if channel == COLLAR:
        return None
    parts = channel.split(".")
    if len(parts) != 3 or parts[0] not in ("observation", "action"):
        return None
    if parts[1] == "images" or parts[2] not in ("pose", "gripper_width"):
        return None
    return parts[1]


def ee_names_of(channels: Iterable[str]) -> tuple[str, ...]:
    """Every end-effector named by ``channels``, in FIRST-SEEN order.

    First-seen, never sorted: the order is the arm order the rest of the
    system uses — the descriptor's declaration order, the dataset's stamp, and
    and the packing order a consumer's channel map uses. A sorted copy agrees
    with those only for as long as every source happens to declare its arms
    alphabetically.
    """
    seen: dict[str, None] = {}
    for channel in channels:
        ee = ee_of(channel)
        if ee is not None:
            seen.setdefault(ee, None)
    return tuple(seen)


def slot_of(channel: str) -> str | None:
    """The camera slot an image channel names, or None."""
    prefix = image("")
    return channel[len(prefix):] if channel.startswith(prefix) else None


def table_channels_for(ee_names: Iterable[str]) -> tuple[str, ...]:
    """Every channel that lives in the PARQUET, for this arm set.

    Free-standing because the parquet's vocabulary is a function of the arms
    ALONE — it needs no camera slots, no grid and no rate. A writer checking
    an episode for completeness would otherwise have to build a whole
    `DatasetSpec` and invent those, and an invented fact in a validation path
    is a validation that lies.
    """
    ees = tuple(ee_names)
    return (
        COLLAR,
        *(ee_pose(e) for e in ees),
        *(action_ee_pose(e) for e in ees),
        *(ee_gripper_width(e) for e in ees),
        *(action_ee_gripper_width(e) for e in ees),
    )


class DatasetError(ValueError):
    """A dataset does not conform to this spec."""


@dataclass(frozen=True)
class Episode:
    """One episode's channels: ``{channel -> (N, D) float32}`` plus its index.

    Storage-agnostic on purpose — a reader of any on-disk format hands episodes
    over in this shape, so nothing downstream has to know how they were stored.
    """

    index: int
    channels: dict[str, np.ndarray]

    @property
    def length(self) -> int:
        return len(next(iter(self.channels.values())))


@dataclass(frozen=True)
class DatasetSpec:
    """Which channels one dataset carries.

    The schema is fixed; the *arity* is not. ``ee_names`` is an ordered tuple of
    end-effector names — two arms, one, or more — and every per-arm channel is
    named after its entry. Fixing the shape is what makes a schema brittle;
    fixing the structure is what makes it a contract.

    ``grid_slot`` names the camera whose frames define the row instants, and
    ``fps`` is how often that camera produced one. They are the same kind of
    fact and belong together: between them they define the grid a row sits on.

    ``grid_slot`` is recorded because the same slot must drive the grid offline
    and online — a dataset whose rows are camera-triggered and a serving loop
    that is free-running would be two different alignment regimes wearing one
    name. ``fps`` is recorded because a consumer replaying the rows as a
    trajectory needs to know what a row's worth of time was; it is provenance,
    not a constraint, and nothing here refuses a dataset for having a different
    one.
    """

    ee_names: tuple[str, ...]
    image_slots: tuple[str, ...]
    grid_slot: str
    fps: float

    def __post_init__(self) -> None:
        if not self.ee_names:
            raise DatasetError("a dataset needs at least one ee")
        for field, values in (
            ("ee_names", self.ee_names),
            ("image_slots", self.image_slots),
        ):
            if len(set(values)) != len(values):
                raise DatasetError(f"duplicate entries in {field}: {values}")
        if self.fps <= 0:
            raise DatasetError(f"fps must be > 0, got {self.fps}")
        if self.grid_slot not in self.image_slots:
            raise DatasetError(
                f"grid_slot {self.grid_slot!r} is not among image_slots "
                f"{list(self.image_slots)} — the grid camera must be recorded"
            )

    # ---- the channel vocabulary -------------------------------------- #

    @property
    def pose_channels(self) -> tuple[str, ...]:
        """Every pose channel, collar first, in a stable order."""
        return (COLLAR, *(ee_pose(e) for e in self.ee_names),
                *(action_ee_pose(e) for e in self.ee_names))

    @property
    def scalar_channels(self) -> tuple[str, ...]:
        """Every gripper-width channel, in a stable order."""
        return (*(ee_gripper_width(e) for e in self.ee_names),
                *(action_ee_gripper_width(e) for e in self.ee_names))

    @property
    def image_channels(self) -> tuple[str, ...]:
        return tuple(image(s) for s in self.image_slots)

    @property
    def table_channels(self) -> tuple[str, ...]:
        """Every channel that lives in the PARQUET.

        Images do not: they are ``.mp4`` files addressed by row, so a caller
        reading `required_channels` off a table gets a `KeyError` for each
        camera. Naming the table's own set removes the chance of asking a
        parquet for a picture.
        """
        return table_channels_for(self.ee_names)

    @property
    def required_channels(self) -> tuple[str, ...]:
        """Everything an episode must carry: the parquet plus the videos.

        Built from `table_channels`, not from `pose_channels` +
        `scalar_channels` again — those two ARE the table, and listing them
        here a second time meant a channel added to `table_channels_for` would
        be silently absent from what this validates against.
        """
        return (*self.table_channels, *self.image_channels)

    def stamp(self) -> dict[str, Any]:
        """The identity keys this spec writes into ``meta/info.json``."""
        return {
            VERSION_KEY: DATASET_VERSION,
            EE_NAMES_KEY: list(self.ee_names),
            GRID_SLOT_KEY: self.grid_slot,
            FPS_KEY: float(self.fps),
        }

    # ---- reading a dataset back -------------------------------------- #

    @classmethod
    def from_stamp(cls, stamp: dict[str, Any], image_slots: Iterable[str]):
        """Rebuild the spec from the identity keys `stamp` wrote, plus the
        camera slots the storage found (fail loud).

        Storage-agnostic: the caller reads the identity keys and the slot list
        out of whatever it stores them in, and this checks they describe a
        dataset this reader implements.

        Verifies the version FIRST: a version mismatch makes every other check
        meaningless, and reporting a missing channel when the real fault is a
        newer schema sends the reader looking in the wrong place.
        """
        version = stamp.get(VERSION_KEY)
        if version is None:
            raise DatasetError(
                f"meta carries no {VERSION_KEY!r} — not a visio dataset "
                "(re-convert it from the source recordings)"
            )
        if version > DATASET_VERSION:
            raise DatasetError(
                f"dataset is spec v{version} but this reader implements "
                f"v{DATASET_VERSION}. Channel additions are additive, but "
                "this reader cannot verify that the addition leaves the "
                "channels it knows unchanged — upgrade visio-schema"
            )
        ee_names = stamp.get(EE_NAMES_KEY)
        if not ee_names:
            raise DatasetError(f"meta carries no {EE_NAMES_KEY!r}")
        grid_slot = stamp.get(GRID_SLOT_KEY)
        if not grid_slot:
            raise DatasetError(f"meta carries no {GRID_SLOT_KEY!r}")
        fps = stamp.get(FPS_KEY)
        if not fps:
            raise DatasetError(
                f"meta carries no {FPS_KEY!r} — the grid rate is what turns a "
                "row count into a duration, and nothing else in the file "
                "records it (timestamps are derived FROM it)"
            )
        return cls(
            ee_names=tuple(ee_names),
            image_slots=tuple(image_slots),
            grid_slot=str(grid_slot),
            fps=float(fps),
        )

    def verify_channels(self, present: Iterable[str]) -> None:
        """Fail loud unless every required channel is in ``present``.

        Names only — a channel's stored WIDTH is a storage fact, checked by
        the storage layer. Unknown names pass: additions are additive by rule,
        so a reader that refused them would break on every forward-compatible
        dataset.
        """
        names = set(present)
        missing = [c for c in self.required_channels if c not in names]
        if missing:
            raise DatasetError(
                f"dataset is missing channel(s) {missing}; "
                f"present: {sorted(names)}"
            )
