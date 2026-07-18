"""breaking_waivers.py is the field-scoped waiver filter behind `make breaking`:
it drops ONLY the specific reviewed field deletions in its WAIVERS table and fails
on every other wire-breaking change. Guard both directions — a real break must not
be swallowed (fail-open), and the sanctioned deletion must pass.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_FILTER = _REPO / "scripts" / "breaking_waivers.py"

_IMU = "proto/visio_schema/v1/calibration/imu.proto"


def _err(path: str, message: str, type_: str = "FIELD_NO_DELETE") -> dict:
    return {"path": path, "type": type_, "start_line": 26, "message": message}


def _deleted(num: int, name: str, message: str = "ImuCalibration") -> dict:
    return _err(_IMU, f'Previously present field "{num}" with name "{name}" '
                      f'on message "{message}" was deleted.')


def _run(errors: list[dict]) -> subprocess.CompletedProcess:
    stdin = "".join(json.dumps(e) + "\n" for e in errors)
    return subprocess.run([sys.executable, str(_FILTER)], input=stdin,
                          capture_output=True, text=True)


def test_empty_input_passes() -> None:
    assert _run([]).returncode == 0


def test_sanctioned_slim_is_waived() -> None:
    # The exact deletions the 0.6.0 ImuCalibration slim makes.
    errors = [_deleted(n, f"f{n}") for n in
              (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 17, 19, 20, 22, 23)]
    r = _run(errors)
    assert r.returncode == 0
    assert "19 error(s) waived" in r.stdout


def test_unwaived_deletion_in_same_message_fails() -> None:
    # A field NOT in the waiver set (14 is a kept field) must survive.
    r = _run([_deleted(14, "accel_noise_density")])
    assert r.returncode == 1
    assert "un-waived" in r.stderr


def test_deletion_in_other_file_fails() -> None:
    r = _run([_err("proto/visio_schema/v1/service/device_info/device_info.proto",
                   'Previously present field "1" with name "id" '
                   'on message "Channel" was deleted.')])
    assert r.returncode == 1


def test_waived_number_in_different_message_fails() -> None:
    # Same file, same number as a waived field, but a DIFFERENT message: the
    # waiver is pinned to ImuCalibration and must not leak to a future message.
    r = _run([_deleted(3, "foo", message="ImuStatus")])
    assert r.returncode == 1
    assert "un-waived" in r.stderr


def test_non_delete_break_is_never_waived() -> None:
    # A type change carries a waived field number but a different rule id.
    r = _run([_err(_IMU, 'Field "3" with name "accel_bias_x" on message '
                         '"ImuCalibration" changed type from "double" to "float".',
                   type_="FIELD_SAME_TYPE")])
    assert r.returncode == 1
