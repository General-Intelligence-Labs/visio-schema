"""The dataset spec is a contract, so its failures are part of it.

These tests pin the two things a contract owes a reader: that a conforming
dataset round-trips through `DatasetSpec.from_stamp`, and that every way of
being non-conforming is rejected with a message naming the actual fault.

Storage-shaped checks — the v2.1 ``features`` encoding, channel widths — are
the storage layer's, and live in `test_lerobot.py`.
"""

import pytest

from visio_schema.dataset import DatasetError, DatasetSpec
from visio_schema.dataset.spec import (
    COLLAR,
    DATASET_VERSION,
    VERSION_KEY,
    action_ee_gripper_width,
    action_ee_pose,
    ee_gripper_width,
    ee_names_of,
    ee_of,
    ee_pose,
    image,
    slot_of,
)


def spec(**kw) -> DatasetSpec:
    return DatasetSpec(
        **{
            "ee_names": ("left", "right"),
            "image_slots": ("head", "left_wrist"),
            "grid_slot": "head",
            **kw,
        }
    )


# ---- the vocabulary ------------------------------------------------- #


def test_channels_cover_collar_both_arms_and_commands():
    s = spec()
    assert COLLAR in s.pose_channels
    for ee in ("left", "right"):
        assert ee_pose(ee) in s.pose_channels
        assert action_ee_pose(ee) in s.pose_channels
        assert ee_gripper_width(ee) in s.scalar_channels
    # collar + 2 measured + 2 commanded
    assert len(s.pose_channels) == 5


def test_arity_is_not_fixed_at_two():
    """Fixing the SHAPE is what makes a schema brittle; the structure is fixed
    and the arity is data."""
    one = spec(ee_names=("right",))
    assert len(one.pose_channels) == 3  # collar + 1 measured + 1 commanded
    three = spec(ee_names=("left", "right", "aux"))
    assert len(three.pose_channels) == 7


# ---- construction guards -------------------------------------------- #


def test_grid_slot_must_be_a_recorded_camera():
    """The grid camera defines the row instants, so a dataset that does not
    carry it has no way to say what its rows mean."""
    with pytest.raises(DatasetError, match="grid_slot"):
        spec(grid_slot="nose_cam")


def test_at_least_one_ee():
    with pytest.raises(DatasetError, match="at least one ee"):
        spec(ee_names=())


def test_duplicate_names_rejected():
    with pytest.raises(DatasetError, match="duplicate"):
        spec(ee_names=("left", "left"))


# ---- identity + channel completeness (storage-agnostic) --------------- #


def test_round_trips_through_its_own_stamp():
    s = spec()
    back = DatasetSpec.from_stamp(s.stamp(), s.image_slots)
    assert back == s


def test_unversioned_meta_is_refused_by_name():
    stamp = spec().stamp()
    del stamp[VERSION_KEY]
    with pytest.raises(DatasetError, match=VERSION_KEY):
        DatasetSpec.from_stamp(stamp, ("head",))


def test_a_newer_dataset_is_refused_rather_than_half_read():
    """Additions are additive, but this reader cannot verify that an addition
    it does not know about leaves the channels it does know unchanged."""
    stamp = {**spec().stamp(), VERSION_KEY: DATASET_VERSION + 1}
    with pytest.raises(DatasetError, match="upgrade visio-schema"):
        DatasetSpec.from_stamp(stamp, ("head",))


def test_version_is_checked_before_anything_else():
    """A version mismatch makes every other check meaningless — reporting a
    missing arm would send the reader looking in the wrong place."""
    stamp = {VERSION_KEY: DATASET_VERSION + 1}          # no ee_names at all
    with pytest.raises(DatasetError, match="spec v"):
        DatasetSpec.from_stamp(stamp, ())


def test_verify_channels_names_the_missing_channel():
    s = spec()
    present = [c for c in s.required_channels if c != ee_pose("right")]
    with pytest.raises(DatasetError, match=ee_pose("right")):
        s.verify_channels(present)


def test_unknown_channels_are_tolerated():
    """Additive-only versioning is worth nothing if readers refuse additions."""
    s = spec()
    s.verify_channels([*s.required_channels, "observation.depth.head"])


# ---- the inverse of the channel constructors --------------------------- #


def test_every_constructor_round_trips_through_ee_of():
    for ee in ("left", "right", "arm_3"):
        for build in (
            ee_pose, ee_gripper_width, action_ee_pose, action_ee_gripper_width
        ):
            assert ee_of(build(ee)) == ee


def test_channels_that_name_no_arm_return_none():
    assert ee_of(COLLAR) is None
    assert ee_of(image("head_camera")) is None
    assert ee_of("timestamp") is None
    assert ee_of("observation.left.velocity") is None


def test_ee_names_are_first_seen_not_sorted():
    """The order IS the arm order the descriptor declared, the dataset
    stamped, and the training map packs. Sorting agrees with those only while
    every source happens to declare its arms alphabetically."""
    assert ee_names_of(
        [ee_pose("right"), COLLAR, ee_pose("left"), action_ee_pose("right")]
    ) == ("right", "left")


def test_slot_of_inverts_image():
    assert slot_of(image("left_gripper")) == "left_gripper"
    assert slot_of(ee_pose("left")) is None
