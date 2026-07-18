#!/usr/bin/env python3
"""Field-scoped waiver filter for `make breaking`.

`buf breaking` (FILE category) is the wire-compat gate. Its FIELD_NO_DELETE rule
fires on ANY removed field, even one whose number AND name are `reserved` — which
is a safe, deliberate schema evolution, not a wire break. We do NOT want to relax
that rule wholesale (a blanket `WIRE_JSON`, or a path-level `ignore`, would let
*future* deletions pass silently too). Instead we waive ONLY the exact, reviewed
deletions below, keyed by rule + file + field number. Everything else — a deletion
of any other field (including a new one in the same file), a renumber, a type
change — still fails the gate.

Usage (wired into the Makefile `breaking` target):
    buf breaking proto --against <baseline> --error-format json | breaking_waivers.py

Reads buf's JSON errors on stdin. Exit 0 if every error is waived (or there are
none); exit 1 and print the survivors otherwise. Waived errors are listed too —
suppression is never silent.

Lifecycle: a waiver is self-expiring. Once its change lands on `main`, the deleted
fields are gone from the breaking baseline and buf stops reporting them, so the
entry matches nothing and is inert. Prune inert entries when convenient; leaving
one in place never widens the gate — it only ever waives those exact fields.
"""

from __future__ import annotations

import json
import re
import sys

# Each waiver authorizes deleting a specific, enumerated set of field NUMBERS from
# ONE named message, for one buf rule. Add an entry only for a reviewed, reserved
# deletion. Matching on the message name (not just the file) matters: a file can
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
]

# buf's JSON error has no structured field/message keys, so scrape them from the
# human-readable message. A wording change → no match → the deletion surfaces as a
# survivor and the gate fails (safe direction), never a silent pass.
#   'Previously present field "12" with name "gyro_scale_z" on message "Foo" was deleted.'
_FIELD_NUM = re.compile(r'field "(\d+)"')
_MESSAGE = re.compile(r'on message "([^"]+)"')


def _waiver_for(err: dict) -> dict | None:
    num_match = _FIELD_NUM.search(err.get("message", ""))
    msg_match = _MESSAGE.search(err.get("message", ""))
    if not num_match or not msg_match:
        return None
    num, message = int(num_match.group(1)), msg_match.group(1)
    for w in WAIVERS:
        if (err.get("type") == w["rule"]
                and err.get("path") == w["path"]
                and message == w["message"]
                and num in w["fields"]):
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
        print(f"make breaking: {len(survivors)} un-waived wire-breaking change(s):", file=sys.stderr)
        for err in survivors:
            loc = f"{err['path']}:{err.get('start_line', '?')}"
            print(f"  {loc}: {err['type']}: {err['message']}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
