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
# Generated bindings now live in-package under python/visio_schema (next to
# the hand-written codec); there is no separate gen/ tree.
PKG_ROOT = REPO_ROOT / "python"

if not (PKG_ROOT / "visio_schema" / "wire" / "v1" / "header_pb2.py").is_file():
    raise SystemExit(f"missing generated bindings under {PKG_ROOT} — run `make gen` first")

try:
    import google.protobuf  # noqa: F401 — presence check only
except ModuleNotFoundError:
    raise SystemExit(
        "missing protobuf runtime — install with `pip install protobuf` "
        "(needed only for the codegen sanity test; `make lint` and `make gen` "
        "do not require it)"
    )

sys.path.insert(0, str(PKG_ROOT))

# Every visio_schema.* module we generate. Listed explicitly (not
# glob-discovered) so a missing file is caught here rather than passing
# silently.
VISIO_MODULES = [
    "visio_schema.wire.v1.header_pb2",
    "visio_schema.sensor.v1.imu_raw_pb2",
    "visio_schema.sensor.v1.encoder_raw_pb2",
    "visio_schema.sensor.v1.system_health_pb2",
    "visio_schema.sensor.v1.audio_compressed_pb2",
    "visio_schema.sensor.v1.button_pb2",
    "visio_schema.calibration.v1.imu_pb2",
    "visio_schema.ros.geometry_msgs.v1.quaternion_pb2",
    "visio_schema.input.v1.quest_controller_state_pb2",
    "visio_schema.geometry.v1.twist_pb2",
    "visio_schema.control.v1.command_pb2",
    "visio_schema.service.device_info.v1.device_info_pb2",
    "visio_schema.service.heartbeat.v1.heartbeat_pb2",
]

# A representative subset of foxglove.* modules we depend on. They ship
# under `visio_schema.foxglove` (the top-level `foxglove` package belongs to the
# official Foxglove SDK); only the python import path is namespaced, the
# protobuf descriptor names stay `foxglove.*`. If foxglove codegen is broken,
# our visio.* modules that import it (via Vector3 / Quaternion) fail above;
# this is a belt-and-suspenders check for the foxglove payload types we use.
FOXGLOVE_MODULES = [
    "visio_schema.foxglove.Vector3_pb2",
    "visio_schema.foxglove.Quaternion_pb2",
    "visio_schema.foxglove.CompressedVideo_pb2",
    "visio_schema.foxglove.PoseInFrame_pb2",
    "visio_schema.foxglove.FrameTransforms_pb2",
    "visio_schema.foxglove.JointStates_pb2",
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
