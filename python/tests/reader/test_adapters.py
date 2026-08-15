"""The wire-schema -> element adapter registry.

Pins the properties the if/elif dispatch chain this replaced had implicitly, plus
the one it could not have: a schema the reader does not know by name can be given
a typed adapter WITHOUT retyping every other consumer's elements.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
import pytest
from _helpers import IMU_RAW, indexed_frames

from visio_schema.reader import (
    IMAGE_SCHEMA,
    IMU_RAW_SCHEMA,
    JOINT_STATES_SCHEMA,
    POSE_SCHEMA,
    VIDEO_SCHEMA,
    Record,
    Session,
)
from visio_schema.reader.adapters import (
    _REGISTRY,
    AdapterContext,
    build_adapter,
    element_adapter,
    registered_schemas,
)

QUAT = "visio_schema.v1.ros.geometry_msgs.Quaternion"


def test_builtins_are_registered():
    """The whole shipped table, pinned by name.

    Adding one is a real behaviour change for every consumer — a topic that used
    to arrive as an opaque `Record` starts arriving typed — so it lands here
    deliberately rather than being noticed downstream.
    """
    assert registered_schemas() == {
        VIDEO_SCHEMA,
        IMAGE_SCHEMA,
        IMU_RAW_SCHEMA,
        POSE_SCHEMA,
        JOINT_STATES_SCHEMA,
    }


def test_unknown_schema_falls_back_to_record():
    """Anything unregistered still streams — parsed, on the same clock, opaque."""
    ctx = AdapterContext(make_decoders=None, message_class_for=lambda n: dict)
    assert type(build_adapter("some.unknown.Schema", ctx)).__name__ == "_RecordAdapter"


def test_duplicate_registration_is_refused():
    """Two adapters for one schema is a silent-corruption bug: whichever import
    ran last would decide the element type. Refuse it at registration."""
    with pytest.raises(ValueError, match="already registered"):
        element_adapter(VIDEO_SCHEMA)(object)


def test_record_adapter_uses_a_fresh_message_per_call():
    """A Record escapes to the consumer and sits in the reorder heap for up to
    `reorder_ns`. A shared parse buffer would alias every queued one to whatever
    was parsed last — this is the invariant that costs an allocation on purpose."""
    from visio_schema import message_class

    ctx = AdapterContext(
        make_decoders=None, message_class_for=lambda n: message_class(QUAT)
    )
    adapter = build_adapter(QUAT, ctx)

    first = message_class(QUAT)()
    first.x = 1.0
    second = message_class(QUAT)()
    second.x = 2.0

    (_, _, rec_a), = adapter.emit(first.SerializeToString(), "t", 10)
    (_, _, rec_b), = adapter.emit(second.SerializeToString(), "t", 20)

    assert rec_a.msg is not rec_b.msg, "shared buffer: the queued Record aliased"
    assert (rec_a.msg.x, rec_b.msg.x) == (1.0, 2.0)


def test_imu_adapter_unbundles_one_message_into_many_samples(rec):
    """The 1->N expansion, and the two clocks: samples sort on their own time,
    the bundle's arrival is what advances the reorder watermark."""
    b = rec("imu.mcap")
    offsets = [0, 1_000_000, 2_000_000]
    b.add_imu_bundle("/ego/imu/0/raw", 5_000, offsets)
    path = b.write()

    samples = list(Session.open(path).stream(["/ego/imu/0/raw"]))
    assert len(samples) == len(offsets)
    assert [s.t_ns for s in samples] == [5_000 + o for o in offsets]


def test_session_override_retypes_one_stream_without_touching_the_registry(rec):
    """The point of the registry. A consumer asks for a richer element type for
    ITS pass; the global table — and therefore every other reader — is unchanged.

    Registering this globally instead would break `visio_body.ingest`, which
    asserts it receives a `Record`.
    """

    class _Typed(NamedTuple):
        """A stand-in for a future Pose/JointState element.

        `topic` and `t_ns` are the element contract — `_reorder` sorts on `t_ns`
        and consumers key on `topic`, so any new element type must carry both.
        """

        topic: str
        t_ns: int
        z: float

    class _TypedAdapter:
        def __init__(self, schema_name, ctx):
            self._cls = ctx.message_class_for(schema_name)

        def emit(self, data, topic, t_ns):
            m = self._cls()
            m.ParseFromString(data)
            yield t_ns, t_ns, _Typed(topic, t_ns, m.z)

        def flush(self):
            return
            yield

    b = rec("quat.mcap")
    b.add_quat("/ego/imu/0/quat", n=3)
    path = b.write()

    before = frozenset(registered_schemas())

    default = list(Session.open(path).stream(["/ego/imu/0/quat"]))
    assert default and all(isinstance(el, Record) for el in default)

    typed = list(
        Session.open(path, adapters={QUAT: _TypedAdapter}).stream(["/ego/imu/0/quat"])
    )
    assert len(typed) == len(default)
    assert all(isinstance(el, _Typed) for el in typed)

    assert registered_schemas() == before, "an override leaked into the global table"
    assert QUAT not in _REGISTRY


def test_video_still_decodes_through_its_adapter(rec):
    """The decoded path end-to-end: the adapter owns the stateful decoder, and a
    frame still comes out with its pixels."""
    b = rec("vid.mcap")
    b.add_camera("/ego/camera/0", indexed_frames(3))
    path = b.write()

    frames = list(Session.open(path).stream(["/ego/camera/0"]))
    assert len(frames) == 3
    assert all(isinstance(f.image, np.ndarray) and f.image.size for f in frames)


def test_imu_raw_constant_matches_the_helpers():
    """The schema names moved to `domain` so `adapters` (below `session`) can see
    them; guard against them drifting from what the builders write."""
    assert IMU_RAW_SCHEMA == IMU_RAW
