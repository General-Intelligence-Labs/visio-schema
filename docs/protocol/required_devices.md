# Required-devices gate

A rig can name the devices that must be attached and ready before a recording
starts. The list is stored on the head, so one firmware image serves every rig
layout, and the gate covers every start path: a host's `StartRecording`, the
head's own button, and a limb button relayed to the head.

## Wire surface

| Where | What |
|---|---|
| `Command.set_required_devices` (`SetRequiredDevices`, tag 56) | Replace the list. An empty list clears it and turns the gate off. |
| `DeviceState.required_devices` (`RequiredDevices`, tag 57) | The list in force. Absent on firmware without the gate, so a host hides the setting. |
| `CaptureState.requirements` (11), `requirements_met` (12) | Each entry judged now: `READY`, `MISSING` or `NOT_READY`, with the satisfying member and a reason. |
| `CaptureState.members` (13) | Every attached member the gate can name (limbs, USB gloves, trackers) with its role, side, unit and readiness: what a host lists when choosing what to require. |
| `CommandResult.error_code = "required_devices_missing"` | A refused start; `error_message` names what is missing. The head also speaks a notice. |

The message definitions live in
[`control/required_devices.proto`](../../proto/visio_schema/v1/control/required_devices.proto).

## Entries

Text form `role[:side][@unit]`, used by the settings QR and by hosts that show
the list:

| Entry | Satisfied by |
|---|---|
| `gripper:left` | any gripper limb calibrated to the left |
| `stereo_addon` | any stereo add-on |
| `glove:right` | whichever glove is on the right hand: a glove board or a USB tactile glove |
| `glove:right@CG020ZLRBB712020` | that tactile glove (its SN), on the right |
| `vive@LHR-0A1B2C3D` | that Vive tracker |

Roles are the `DeviceInfo.equipment_type` values limbs announce (`gripper`,
`glove`, `wrist`, `stereo_addon`) plus the head-local `vive`. Sides are `left`
and `right`, and only `gripper`, `glove` and `wrist` take one; an entry without
a side matches either. The unit is a limb's `device_name`, a tactile glove's SN
or a tracker's serial, and holds no whitespace, `@` or `:`. A limb whose side
was never assigned is required by its unit (`wrist@GILABS-RPi82Fog`). At most
16 entries. [`tests/golden/required_device_vectors.txt`](../../tests/golden/required_device_vectors.txt)
is the grammar as cases; the generator, the firmware and the app each replay it.

Entries need not be attached when the list is set: a settings QR can provision a
rig before it is assembled.

## Readiness

| Member | Ready when |
|---|---|
| Limb board | linked, identified by its `DeviceInfo`, and sensor samples (video, IMU, audio, encoder, tactile) received on its link within 500 ms; its 1 Hz status alone does not count |
| USB tactile glove | bound to its side, with its layout loaded and a frame newer than 500 ms |
| Vive tracker | `TRACKING`, clock synchronized, station map ready, reported by the bridge within 3 s |

`RequirementState.State` 0 is `STATE_UNSPECIFIED` and never sent, so a verdict
left unset never reads as ready. `detail` is English for logs and support; a
host shows its own words for the state.

A limb board attaching or detaching during a recording ends that recording
(`RIG_MEMBERSHIP_CHANGED`), whether or not it is required. A USB glove or a
tracker does not: they are not capture-roster members, and trackers sleep and
wake during a take. The gate only decides whether a recording may start.

A stored list the device cannot read holds every start
(`required_devices_missing`) until a list is set again, and is echoed as empty.

## Settings QR

The `visio-settings` payload (v1 and v2) takes an optional top-level key:

```json
{ "t": "visio-settings", "v": 2,
  "required_devices": ["gripper:left", "gripper:right", "glove:left", "glove:right"] }
```

Absent keeps the device's list; a list replaces it; `[]` clears it. The
generator (`visio-settings-qr`) validates the grammar and asks for the list in
its guided flow. An app that predates the key ignores it with a warning and
applies the rest.
