"""RerunSink's SystemHealth rendering.

``RerunSink.__init__`` imports PyAV and rerun and spawns a viewer, so these
build the object with ``__new__`` and inject a recording stand-in for ``rr``.
That keeps the subject the mapping from protobuf fields to Rerun entities,
which is the part that can silently stop plotting a field.
"""
from __future__ import annotations

import pytest

from visio_schema.display import RerunSink
from visio_schema.v1.sensor.system_health_pb2 import SystemHealth


class _FakeRr:
    """Records ``log(entity, Scalars(value))`` calls."""

    def __init__(self) -> None:
        self.logged: dict[str, float] = {}

    @staticmethod
    def Scalars(value):   # casing mirrors rerun's own API
        return value

    def log(self, entity, value) -> None:
        self.logged[entity] = value


def _render(health: SystemHealth, topic: str = "/ego/system_health") -> dict:
    sink = RerunSink.__new__(RerunSink)
    sink._rr = _FakeRr()
    sink._health_topic = None
    sink._dirty = False
    msg = type("M", (), {"payload": health.SerializeToString()})()
    sink._log_health(msg, topic)
    return sink._rr.logged


def test_camera_temps_log_one_entity_per_reporting_camera() -> None:
    h = SystemHealth(cpu_temp_c=101.5)
    h.camera_temps.add(index=0, sensor_temp_c=88.0)
    h.camera_temps.add(index=3, sensor_temp_c=89.5)

    logged = _render(h)

    assert logged["ego/system_health/cpu_temp_c"] == pytest.approx(101.5)
    assert logged["ego/system_health/camera/0/sensor_temp_c"] == pytest.approx(88.0)
    assert logged["ego/system_health/camera/3/sensor_temp_c"] == pytest.approx(89.5)


def test_camera_temps_keyed_by_index_not_position() -> None:
    h = SystemHealth()
    h.camera_temps.add(index=1, sensor_temp_c=64.0)

    logged = _render(h)

    assert "ego/system_health/camera/0/sensor_temp_c" not in logged
    assert logged["ego/system_health/camera/1/sensor_temp_c"] == pytest.approx(64.0)


def test_no_camera_temps_logs_nothing_for_cameras() -> None:
    # A board whose sensors have no temperature register reports the SoC
    # only; absent must not become zero.
    logged = _render(SystemHealth(cpu_temp_c=71.0))

    assert logged == {"ego/system_health/cpu_temp_c": pytest.approx(71.0)}
