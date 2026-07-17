"""RelayRegistry — reconnect tolerance for a relayed multiplex stream.

A relay leg (e.g. the Quest :50002 TCP stream) multiplexes many devices onto one
link with no per-device link-drop, so a reconnecting device re-announces its
topics under new ids. The strict single-source ChannelRegistry rejects those as
duplicate-topic; RelayRegistry adopts them (same device) while still surfacing a
genuine collision (different device claiming a live topic).
"""
from __future__ import annotations

import logging

from visio_schema.display.relay_registry import RelayRegistry
from visio_schema.v1.service.device_info.device_info_pb2 import Channel, DeviceInfo
from visio_schema.wire.control import DEVICE_INFO
from visio_schema.wire.message import Message

_ENC = "visio_schema.v1.sensor.EncoderRaw"


def _channel(cid: int, topic: str) -> Channel:
    return Channel(id=cid, topic=topic, encoding="protobuf",
                   schema_name=_ENC, schema_encoding="protobuf")


def _announce(device_name: str, *channels: Channel) -> Message:
    return Message(stream_id=DEVICE_INFO,
                   payload=DeviceInfo(device_name=device_name,
                                      channels=channels).SerializeToString())


def _data(cid: int, payload: bytes = b"x") -> Message:
    return Message(stream_id=cid, payload=payload)


def test_reconnect_same_device_adopts_new_id(caplog) -> None:
    reg = RelayRegistry()
    reg.accept(_announce("gripL", _channel(74, "/gripper_left/camera/0")))
    assert reg.resolve(74).topic == "/gripper_left/camera/0"

    # Same device reconnects: same topic, NEW id (74 is dead).
    with caplog.at_level(logging.WARNING):
        reg.accept(_announce("gripL", _channel(92, "/gripper_left/camera/0")))
    assert reg.resolve(92).topic == "/gripper_left/camera/0"   # new id adopted
    assert reg.resolve(74) is None                              # stale id forgotten
    assert "reconnected" in caplog.text and "74 -> 92" in caplog.text  # warned

    # Data on the new id now resolves — the actual Foxglove-facing fix.
    routed = reg.accept(_data(92))
    assert routed.channel is not None
    assert routed.channel.topic == "/gripper_left/camera/0"


def test_cross_device_collision_still_surfaced(caplog) -> None:
    reg = RelayRegistry()
    reg.accept(_announce("gripL_A", _channel(74, "/gripper_left/camera/0")))
    with caplog.at_level(logging.ERROR):
        # A DIFFERENT device claims the same live topic (two same-side grippers).
        reg.accept(_announce("gripL_B", _channel(92, "/gripper_left/camera/0")))

    # First (live) owner keeps the topic; the colliding id is NOT learned.
    assert reg.resolve(74).topic == "/gripper_left/camera/0"
    assert reg.resolve(92) is None
    assert "refusing to also map" in caplog.text   # diagnostic preserved


def test_same_device_same_id_is_idempotent() -> None:
    reg = RelayRegistry()
    reg.accept(_announce("gripL", _channel(74, "/gripper_left/camera/0")))
    reg.accept(_announce("gripL", _channel(74, "/gripper_left/camera/0")))
    assert reg.resolve(74).topic == "/gripper_left/camera/0"


def test_resolved_stream_delivers_reconnected_device_data() -> None:
    # End-to-end via resolved(): a data frame before AND after a reconnect (with
    # a new id) both come through.
    reg = RelayRegistry()
    stream = [
        _announce("gripR", _channel(50, "/gripper_right/encoder/0/raw")),
        _data(50, b"a"),
        _announce("gripR", _channel(88, "/gripper_right/encoder/0/raw")),  # reconnect
        _data(88, b"b"),
    ]
    rows = list(reg.resolved(stream))
    assert [ch.topic for _, ch in rows] == [
        "/gripper_right/encoder/0/raw",
        "/gripper_right/encoder/0/raw",
    ]
    assert [m.payload for m, _ in rows] == [b"a", b"b"]  # both delivered


def test_distinct_devices_distinct_topics_coexist() -> None:
    # The common healthy case: two grippers, different sides, both resolve.
    reg = RelayRegistry()
    reg.accept(_announce("gripL", _channel(74, "/gripper_left/encoder/0/raw")))
    reg.accept(_announce("gripR", _channel(75, "/gripper_right/encoder/0/raw")))
    assert reg.resolve(74).topic == "/gripper_left/encoder/0/raw"
    assert reg.resolve(75).topic == "/gripper_right/encoder/0/raw"
