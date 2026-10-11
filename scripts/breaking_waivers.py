#!/usr/bin/env python3
"""Field-scoped waiver filter for `make breaking`.

`buf breaking` (FILE category) is the wire-compat gate. Some of its rules fire
on safe, deliberate schema evolution: FIELD_NO_DELETE on a field whose number and
name are `reserved`, FIELD_SAME_CARDINALITY on a field gaining explicit presence
(same wire encoding). We do NOT want to relax those rules wholesale (a blanket
`WIRE_JSON`, or a path-level `ignore`, would let *future* changes pass silently
too). Instead we waive ONLY the exact, reviewed changes below, keyed by rule +
file + message + field number (+ the transition, where a rule covers both
directions). Everything else — a change to any other field (including a new one
in the same file), a renumber, a type change — still fails the gate.

Usage (wired into the Makefile `breaking` target):
    buf breaking proto --against <baseline> --error-format json | breaking_waivers.py

Reads buf's JSON errors on stdin. Exit 0 if every error is waived (or there are
none); exit 1 and print the survivors otherwise. Waived errors are listed too —
suppression is never silent.

Lifecycle: a waiver is self-expiring. Once its change lands on `main`, it is
part of the breaking baseline and buf stops reporting it, so the entry matches
nothing and is inert. Prune inert entries when convenient; leaving
one in place never widens the gate — it only ever waives those exact fields.
"""

from __future__ import annotations

import json
import re
import sys

# Each waiver authorizes one buf rule for a specific, enumerated set of field
# NUMBERS in ONE named message. Add an entry only for a reviewed change: a
# reserved deletion, or a presence change the CHANGELOG proves wire-compatible.
# Matching on the message name (not just the file) matters: a file can
# grow a second message, and a waived number could recur there — the message name
# keeps the waiver pinned to the message it was reviewed against.
WAIVERS: list[dict] = [
    {
        "rule": "FIELD_NO_DELETE",
        "path": "proto/visio_schema/v1/calibration/imu.proto",
        "message": "ImuCalibration",
        # The full `reserved` block of the 0.6.0 slim (imu.proto):
        # 1-12  per-axis accel/gyro bias + scale (bias is runtime state; scale is
        #       the redundant diagonal of the misalignment matrix)
        # 13    mounting_pose (now on the /imu/<i>/extrinsics topic)
        # 15,17 accel/gyro bias random walk (host/vibration dependent, not per-unit)
        # 19,20,22,23  misalignment / g-sensitivity / gyro->accel rotation
        #              (deterministic intrinsics, factory-trimmed)
        "fields": {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 17, 19, 20, 22, 23},
        "reason": "0.6.0 ImuCalibration slim — numbers + names reserved in imu.proto; "
                  "see CHANGELOG 0.6.0.",
    },
    {
        "rule": "FIELD_NO_DELETE",
        "path": "proto/visio_schema/v1/sensor/camera_frame_info.proto",
        "message": "CameraFrameInfo",
        # The 0.7.0 single-join-key trim (camera_frame_info.proto):
        # 2  frame_id    — a coordinate frame NAME copied from the sibling video
        #                  topic, but exposure metadata is not expressed in any
        #                  frame, so it anchored nothing.
        # 4  vi_time_ref — the drain frame's counter, published so that
        #                  `vi_time_ref != isp_frame_id` could flag a
        #                  substitute-stamped entry. The producer no longer
        #                  substitute-stamps, and on real recordings that flag was
        #                  aliased with the healthy case anyway.
        "fields": {2, 4},
        "reason": "0.7.0 CameraFrameInfo single-join-key trim — numbers + names "
                  "reserved; see CHANGELOG 0.7.0.",
    },
    {
        "rule": "FIELD_NO_DELETE",
        "path": "proto/visio_schema/v1/sensor/camera_frame_info.proto",
        "message": "CameraFrameInfo",
        # 0.10.0: the raw per-frame fields (3, 5-13) are replaced by
        # producer-computed exposure duration, midpoint offset, total gain,
        # line delay and readout direction (fields 14-18). `timestamp` (1) and
        # the join rule are unchanged; 2-13 are reserved by number and name.
        "fields": {3, 5, 6, 7, 8, 9, 10, 11, 12, 13},
        "reason": "0.10.0 CameraFrameInfo carries producer-computed exposure "
                  "timing — numbers + names reserved; see CHANGELOG 0.10.0.",
    },
    # 0.10.0 one settings rule: these fields gained explicit presence
    # (`optional`). Same wire encoding — buf flags it because generated code
    # gains `has_*` — and an old sender's empty fields read as absent, which
    # keeps the stored value. See CHANGELOG 0.10.0. Pinned to that direction:
    # the rule also fires on explicit -> implicit, which IS a behaviour break.
    *(
        {
            "rule": "FIELD_SAME_CARDINALITY",
            "path": "proto/visio_schema/v1/control/command.proto",
            "message": message,
            "fields": fields,
            "transition": 'from "optional with implicit presence" '
                          'to "optional with explicit presence"',
            "reason": "0.10.0 one settings rule — implicit to explicit "
                      "presence, wire-compatible; see CHANGELOG 0.10.0.",
        }
        for message, fields in (
            ("SetRecordingMeta", {1, 2, 3, 4, 5, 6, 7, 8}),
            ("SetTime", {3, 4}),
            ("SetStorage", {1, 2, 3, 4, 5, 6, 7}),
            ("TestStorage", {1, 2, 3, 4, 5, 6, 7}),
        )
    ),
]

# buf's JSON error has no structured field/message keys, so scrape them from the
# human-readable message. A wording change → no match → the deletion surfaces as a
# survivor and the gate fails (safe direction), never a silent pass.
#   'Previously present field "12" with name "gyro_scale_z" on message "Foo" was deleted.'
_FIELD_NUM = re.compile(r'field "(\d+)"', re.IGNORECASE)
_MESSAGE = re.compile(r'on message "([^"]+)"')


def _waiver_for(err: dict) -> dict | None:
    text = err.get("message", "")
    num_match = _FIELD_NUM.search(text)
    msg_match = _MESSAGE.search(text)
    if not num_match or not msg_match:
        return None
    num, message = int(num_match.group(1)), msg_match.group(1)
    for w in WAIVERS:
        if (err.get("type") == w["rule"]
                and err.get("path") == w["path"]
                and message == w["message"]
                and num in w["fields"]
                and w.get("transition", "") in text):
            return w
    return None


def main() -> int:
    errors = [json.loads(line) for line in sys.stdin if line.strip()]
    if not errors:
        return 0

    waived, survivors = [], []
    for err in errors:
        (waived if _waiver_for(err) else survivors).append(err)

    if waived:
        print(f"make breaking: {len(waived)} error(s) waived by scripts/breaking_waivers.py:")
        for err in waived:
            print(f"  [waived] {err['path']}: {err['message']}")

    if survivors:
        print(f"make breaking: {len(survivors)} un-waived wire-breaking "
              "change(s):", file=sys.stderr)
        for err in survivors:
            loc = f"{err['path']}:{err.get('start_line', '?')}"
            print(f"  {loc}: {err['type']}: {err['message']}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
