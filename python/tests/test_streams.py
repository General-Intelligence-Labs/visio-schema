"""Tests for the wire.streams schema-pool helpers (proto type name -> class /
serialized FileDescriptorSet)."""
from __future__ import annotations

import pytest
from google.protobuf import descriptor_pb2

from visio_schema.wire import streams


def test_message_class_known_type():
    """message_class resolves a fully-qualified proto type to its message class."""
    cls = streams.message_class("visio_schema.sensor.v1.ImuRaw")
    assert cls.__name__ == "ImuRaw"


def test_message_class_unknown_raises():
    """message_class raises for an unknown type name."""
    with pytest.raises(KeyError):
        streams.message_class("visio_schema.nope.v1.DoesNotExist")


def test_file_descriptor_set_nonempty():
    """file_descriptor_set returns a non-empty serialized FDS for a known type."""
    fds = streams.file_descriptor_set("visio_schema.sensor.v1.ImuRaw")
    assert len(fds) > 0


def test_file_descriptor_set_deps_included():
    """The FDS includes the type's own file plus transitive deps."""
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(streams.file_descriptor_set("visio_schema.sensor.v1.ImuRaw"))
    names = {f.name for f in fds.file}
    assert any("imu_raw" in n for n in names)
    assert len(fds.file) >= 1


def test_file_descriptor_set_distinct_per_type():
    """Distinct types produce distinct descriptor sets (no cache collision)."""
    imu = streams.file_descriptor_set("visio_schema.sensor.v1.ImuRaw")
    video = streams.file_descriptor_set("foxglove.CompressedVideo")
    assert imu != video


def test_file_descriptor_set_decodes_target_type():
    """The FDS actually carries the requested message type."""
    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(streams.file_descriptor_set("foxglove.CompressedVideo"))
    msgs = {m.name for f in fds.file for m in f.message_type}
    assert "CompressedVideo" in msgs
