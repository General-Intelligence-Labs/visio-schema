"""ChannelRegistry — the single-source topic/schema table (no bus).

Own outputs (declare), learned channels (from announces, by id), the unique-topic
invariant, forget, and the no-bus `accept`/`resolved` consume path. The
per-endpoint remap that used to live here is now the bus's job, so there is no
`source_key`.
"""
from __future__ import annotations

import pytest

from visio_schema.routing import (
    FIRST_DYNAMIC,
    ChannelRegistry,
    DuplicateTopicError,
    Routed,
)
from visio_schema.v1.service.device_info.device_info_pb2 import Channel
from visio_schema.wire.control import (
    COMMAND,
    DEVICE_INFO,
    HEARTBEAT,
    LINK_LOCAL_CONTROL,
)
from visio_schema.wire.message import Message

_IMU = "visio_schema.v1.sensor.ImuRaw"
_QUAT = "visio_schema.v1.ros.geometry_msgs.Quaternion"


def _channel(cid: int, topic: str, schema_name: str = _IMU) -> Channel:
    return Channel(id=cid, topic=topic, encoding="protobuf",
                   schema_name=schema_name, schema_encoding="protobuf")


def _announce(*channels: Channel) -> Message:
    from visio_schema.v1.service.device_info.device_info_pb2 import DeviceInfo
    return Message(stream_id=1, payload=DeviceInfo(channels=channels).SerializeToString())


# ── Own outputs ─────────────────────────────────────────────────────────────

def test_declare_idempotent_from_first_dynamic() -> None:
    reg = ChannelRegistry("dev")
    a = reg.declare("/dev/imus/0/raw", _IMU)
    b = reg.declare("/dev/imus/1/raw", _IMU)
    assert a == FIRST_DYNAMIC and b == FIRST_DYNAMIC + 1
    assert reg.declare("/dev/imus/0/raw", _IMU) == a          # idempotent
    assert reg.local_id_for("/dev/imus/0/raw") == a
    assert {c.topic for c in reg.own_channels()} == {"/dev/imus/0/raw", "/dev/imus/1/raw"}


def test_declare_output_self_describes() -> None:
    reg = ChannelRegistry("dev")
    cid = reg.declare_output("/dev/imus/0/raw", _IMU)
    ch = reg.resolve(cid)
    assert ch.schema_name == _IMU and len(ch.schema) > 0


# ── Learned channels ─────────────────────────────────────────────────────────

def test_learn_by_id_and_resolve() -> None:
    reg = ChannelRegistry()
    reg.learn(_channel(16, "/c/imu/0/raw"))
    ch = reg.resolve(16)
    assert ch is not None and ch.topic == "/c/imu/0/raw"
    assert [c.topic for c in reg.channels()] == ["/c/imu/0/raw"]


def test_learn_same_id_is_idempotent() -> None:
    reg = ChannelRegistry()
    reg.learn(_channel(16, "/c/imu/0/raw"))
    reg.learn(_channel(16, "/c/imu/0/raw"))   # re-announce, same id
    assert reg.resolve(16).topic == "/c/imu/0/raw"


def test_duplicate_topic_raises() -> None:
    reg = ChannelRegistry()
    reg.learn(_channel(16, "/c/imu/0/raw"))
    with pytest.raises(DuplicateTopicError):
        reg.learn(_channel(17, "/c/imu/0/raw"))   # same topic, different id


def test_forget_frees_id_and_topic() -> None:
    reg = ChannelRegistry()
    reg.learn(_channel(16, "/c/imu/0/raw"))
    reg.forget([16])
    assert reg.resolve(16) is None
    # the topic frees up: a reconnect may re-announce it under a new id
    reg.learn(_channel(99, "/c/imu/0/raw"))
    assert reg.resolve(99).topic == "/c/imu/0/raw"


# ── Inbound consume path (no bus) ────────────────────────────────────────────

def test_accept_learns_announce_and_absorbs() -> None:
    reg = ChannelRegistry()
    assert reg.accept(_announce(_channel(16, "/c/imu/0/raw"))) == Routed(None, None)
    assert reg.resolve(16).topic == "/c/imu/0/raw"


def test_accept_resolves_data_and_drops_until_known() -> None:
    reg = ChannelRegistry()
    assert reg.accept(Message(stream_id=16, payload=b"x")) == Routed(None, None)
    assert reg.dropped_unmapped == 1
    reg.accept(_announce(_channel(16, "/c/imu/0/raw")))
    msg, ch = reg.accept(Message(stream_id=16, payload=b"y", seq=3))
    assert ch is not None and ch.topic == "/c/imu/0/raw"
    assert msg.payload == b"y" and msg.seq == 3


def test_accept_passes_other_control() -> None:
    reg = ChannelRegistry()
    hb = Message(stream_id=3, payload=b"b")     # HEARTBEAT
    assert reg.accept(hb) == Routed(hb, None)


def test_resolved_yields_only_data_rows() -> None:
    reg = ChannelRegistry()
    reg.accept(_announce(_channel(16, "/c/imu/0/raw")))
    stream = [
        Message(stream_id=16, payload=b"a"),
        Message(stream_id=3, payload=b"hb"),       # control: skipped
        Message(stream_id=99),                     # unknown data: skipped
        Message(stream_id=16, payload=b"b"),
    ]
    rows = list(reg.resolved(stream))
    assert [m.payload for m, _ in rows] == [b"a", b"b"]
    assert all(c.topic == "/c/imu/0/raw" for _, c in rows)


# ── Discovery ────────────────────────────────────────────────────────────────

def test_self_info_is_own_only() -> None:
    # self_info announces OWN outputs only; learned channels propagate by the bus
    # forwarding each leaf's announce, not by recombining them here. The learned
    # channel stays resolvable (it is in by_id), just not re-announced.
    reg = ChannelRegistry("hub")
    reg.declare_output("/hub/imus/0/raw", _IMU)
    learned_id = reg.alloc()
    reg.learn(_channel(learned_id, "/child/imus/0/quat", _QUAT))  # global id, no collision
    di = reg.self_info()
    assert di.device_name == "hub"
    assert {c.topic for c in di.channels} == {"/hub/imus/0/raw"}   # own only
    assert reg.resolve(learned_id).topic == "/child/imus/0/quat"  # still resolvable


def test_well_known_device_info_channel_resolves() -> None:
    # The DeviceInfo control stream resolves to a built-in well-known channel so a
    # recorder can write forwarded announces on "/device_info" — without it being
    # an own output or appearing in channels().
    reg = ChannelRegistry("hub")
    ch = reg.resolve(DEVICE_INFO)
    assert ch is not None
    assert ch.topic == "/device_info"
    assert ch.schema_name == "visio_schema.v1.service.device_info.DeviceInfo"
    assert ch.schema                       # carries the FileDescriptorSet
    assert reg.channels() == []            # not an own/learned data channel
    assert reg.has_own_outputs is False


def test_link_local_control_membership() -> None:
    # Disposition is structural: heartbeat is link-scoped (dropped at a hop);
    # device_info/command are end-to-end (forwarded).
    assert LINK_LOCAL_CONTROL == {HEARTBEAT}
    assert DEVICE_INFO not in LINK_LOCAL_CONTROL
    assert COMMAND not in LINK_LOCAL_CONTROL
