"""Codegen sanity test: every generated Python module must import cleanly.

If a .proto file has a syntax problem, a bad import, or a name collision,
this catches it before downstream consumers do. Run via `make test`
(which runs `make gen` first).

Prerequisites:
  pip install protobuf
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GEN_PYTHON = REPO_ROOT / "gen" / "python"

if not GEN_PYTHON.is_dir():
    raise SystemExit(f"missing {GEN_PYTHON} — run `make gen` first")

try:
    import google.protobuf  # noqa: F401 — presence check only
except ModuleNotFoundError:
    raise SystemExit(
        "missing protobuf runtime — install with `pip install protobuf` "
        "(needed only for the codegen sanity test; `make lint` and `make gen` "
        "do not require it)"
    )

sys.path.insert(0, str(GEN_PYTHON))

# Every visio.* module we generate. Listed explicitly (not glob-discovered)
# so a missing file is caught here rather than passing silently.
VISIO_MODULES = [
    "visio.wire.v1.header_pb2",
    "visio.sensor.v1.imu_raw_pb2",
    "visio.sensor.v1.encoder_raw_pb2",
    "visio.sensor.v1.system_health_pb2",
    "visio.sensor.v1.audio_compressed_pb2",
    "visio.sensor.v1.button_pb2",
    "visio.ros.geometry_msgs.v1.quaternion_pb2",
    "visio.input.v1.quest_controller_state_pb2",
    "visio.geometry.v1.twist_pb2",
    "visio.control.v1.command_pb2",
    "visio.service.timesync.v1.timesync_pb2",
    "visio.service.device_info.v1.device_info_pb2",
    "visio.service.heartbeat.v1.heartbeat_pb2",
    "visio.service.schema.v1.schema_pb2",
]

# A representative subset of foxglove.* modules we depend on. If foxglove
# codegen is broken, our visio.* modules that import it (via Vector3 /
# Quaternion) will fail above; this list is a belt-and-suspenders check
# for the ones we explicitly reference in StreamKind mappings.
FOXGLOVE_MODULES = [
    "foxglove.Vector3_pb2",
    "foxglove.Quaternion_pb2",
    "foxglove.CompressedVideo_pb2",
    "foxglove.PoseInFrame_pb2",
    "foxglove.FrameTransforms_pb2",
    "foxglove.JointStates_pb2",
]

failures: list[tuple[str, str]] = []
for module_name in VISIO_MODULES + FOXGLOVE_MODULES:
    try:
        import_module(module_name)
    except Exception as exc:  # noqa: BLE001 — we want every failure
        failures.append((module_name, f"{type(exc).__name__}: {exc}"))

if failures:
    print(f"FAIL: {len(failures)} module(s) did not import", file=sys.stderr)
    for module_name, reason in failures:
        print(f"  - {module_name}: {reason}", file=sys.stderr)
    raise SystemExit(1)

print(f"OK: imported {len(VISIO_MODULES) + len(FOXGLOVE_MODULES)} modules")
