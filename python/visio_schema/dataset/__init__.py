"""The visio dataset layer: a fixed episode spec + its on-disk formats.

`spec` says WHAT a dataset carries (channels, units, frames, versioning) and is
storage-agnostic; `lerobot` implements it as a LeRobot v2.1 directory; `video`
encodes the per-slot mp4s. Import from this package root — the deeper module
layout is not a stability surface.

Only `spec` is imported eagerly. The storage and encode halves pull pyarrow and
av, which a consumer that just wants the channel vocabulary should not pay for
(a serving process reads no parquet and encodes no video), so they resolve on
first attribute access. `from visio_schema.dataset import LeRobotDataset` works
exactly as if they were imported here.

Not part of the package facade (`visio_schema.__all__`): like
`visio_schema.reader`, this layer is importable directly and versioned by the
package as a whole.
"""

from typing import TYPE_CHECKING

from visio_schema.dataset.spec import (
    COLLAR,
    DATASET_VERSION,
    POSE_DIM,
    DatasetError,
    DatasetSpec,
    Episode,
    action_ee_gripper_width,
    action_ee_pose,
    ee_gripper_width,
    ee_names_of,
    ee_of,
    ee_pose,
    image,
    slot_of,
)

if TYPE_CHECKING:  # for type checkers only — no import cost at runtime
    from visio_schema.dataset.geometry import scaled_dims
    from visio_schema.dataset.lerobot import (
        LeRobotDataset,
        features,
        spec_from_info,
    )
    from visio_schema.dataset.video import (
        Mp4Writer,
        SlotWriters,
        VideoEncodeParams,
    )

# attribute -> the submodule that defines it (PEP 562 deferred import)
_DEFERRED = {
    "LeRobotDataset": "lerobot",
    "features": "lerobot",
    "spec_from_info": "lerobot",
    "Mp4Writer": "video",
    "SlotWriters": "video",
    "VideoEncodeParams": "video",
    "scaled_dims": "geometry",
}


def __getattr__(name: str):
    module = _DEFERRED.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(
        importlib.import_module(f"visio_schema.dataset.{module}"), name
    )
    globals()[name] = value  # resolve once; later reads hit the module dict
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "COLLAR",
    "DATASET_VERSION",
    "POSE_DIM",
    "DatasetError",
    "DatasetSpec",
    "Episode",
    "LeRobotDataset",
    "Mp4Writer",
    "SlotWriters",
    "VideoEncodeParams",
    "action_ee_gripper_width",
    "action_ee_pose",
    "ee_gripper_width",
    "ee_names_of",
    "ee_of",
    "ee_pose",
    "features",
    "image",
    "scaled_dims",
    "slot_of",
    "spec_from_info",
]
