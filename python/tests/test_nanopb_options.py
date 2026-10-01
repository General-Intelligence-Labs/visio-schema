"""nanopb.options sizes the FIRMWARE's static decode buffers, and getting a cap
wrong is silent on both ends: nanopb does not truncate an oversized string, it
fails pb_decode, so the device discards the whole Command and answers nothing.
The app then waits out its own timeout with nothing to report.

That shipped once — a 36-character Tencent COS SecretId against `max_size:32`
made Tencent destinations impossible to set, while Aliyun (24) and AWS (20) fit
and hid it. The invariants below are the ones stated in that file's comments;
this is what makes them true rather than aspirational.

buf never reads this file (`make lint`/`make breaking` are proto operations), so
without these tests the caps are pinned by nothing that CI runs. The companion app
carries its own copy of the storage caps and cross-checks it against this file,
but only when the sibling checkout is present — which its CI does not do.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_OPTIONS = Path(__file__).resolve().parents[2] / "proto" / "nanopb.options"

_PREFIX = "visio_schema.v1.control."

# 36 chars is a real Tencent COS SecretId; +1 for the NUL max_size counts.
_TENCENT_SECRET_ID_LEN = 36


def _max_sizes() -> dict[str, int]:
    """Every `<message>.<field> max_size:N` in the file, keyed `Message.field`."""
    out: dict[str, int] = {}
    for line in _OPTIONS.read_text().splitlines():
        line = line.strip()
        if not line.startswith(_PREFIX):
            continue
        m = re.match(r"(\S+)\s+.*max_size:(\d+)", line)
        if m:
            out[m.group(1)[len(_PREFIX) :]] = int(m.group(2))
    return out


@pytest.fixture(scope="module")
def sizes() -> dict[str, int]:
    caps = _max_sizes()
    assert caps, f"parsed no max_size entries from {_OPTIONS}"
    return caps


# SetStorage applies a destination and TestStorage probes the same one; a
# credential the device will accept must also be one it can be asked to test.
def test_set_and_test_storage_caps_agree(sizes: dict[str, int]) -> None:
    fields = [k.split(".", 1)[1] for k in sizes if k.startswith("SetStorage.")]
    assert fields, "no SetStorage max_size entries"
    for field in fields:
        assert sizes[f"TestStorage.{field}"] == sizes[f"SetStorage.{field}"], (
            f"SetStorage.{field} and TestStorage.{field} must have the same cap"
        )


# The device echoes the accepted key back in DeviceState and the app reads that
# into its form; a smaller cap here would silently truncate what was just set.
def test_device_state_can_report_the_key_it_accepted(sizes: dict[str, int]) -> None:
    assert sizes["DeviceState.storage_access_key_id"] == sizes["SetStorage.access_key_id"]


def test_access_key_id_fits_a_tencent_secret_id(sizes: dict[str, int]) -> None:
    # max_size counts the NUL, so the usable length is one less.
    assert sizes["SetStorage.access_key_id"] - 1 >= _TENCENT_SECRET_ID_LEN


# The QR generator refuses to print a field longer than the device can decode,
# using its own copy of these caps (DEVICE_MAX_SIZE). It is the same number in
# two files, and only this test stops them drifting — at which point the
# generator would print a code every device silently discards, exactly the
# capturer=64-bytes bug that motivated the check.
def test_generator_field_caps_match_the_options(sizes: dict[str, int]) -> None:
    from visio_schema.settings_qr.payload import DEVICE_MAX_SIZE

    for field, cap in DEVICE_MAX_SIZE.items():
        assert sizes[field] == cap, (
            f"{field} max_size:{sizes.get(field)} != DEVICE_MAX_SIZE ({cap})"
        )


# A string OR bytes field with no max_size becomes a pb_callback_t, which the
# firmware's static decode path cannot use — the field silently never arrives.
# Bytes matter as much as strings here: the sealed provisioning envelope is a
# `bytes` field, and an unsized one would make a v2 settings QR appear to apply
# while setting nothing at all.
@pytest.mark.parametrize("message",
                         ["SetStorage", "TestStorage", "SetRecordingKey",
                          "ListRecordings", "OpenRecordingFile", "DeleteRecording"])
def test_every_inbound_field_is_sized(sizes: dict[str, int], message: str) -> None:
    from visio_schema.v1.control import command_pb2

    descriptor = getattr(command_pb2, message).DESCRIPTOR
    for field in descriptor.fields:
        if field.type in (field.TYPE_STRING, field.TYPE_BYTES):
            assert f"{message}.{field.name}" in sizes, (
                f"{message}.{field.name} has no max_size — it would decode as a callback"
            )


# The sealed envelope caps are a MEASUREMENT, not a guess: this builds the
# largest body the generator can actually emit and checks it fits. A cap set
# too low does not truncate — nanopb fails the whole decode, so the device
# discards the Command and answers nothing, exactly the Tencent failure above.
def test_sealed_cap_matches_the_generators_enforced_bound(
        sizes: dict[str, int]) -> None:
    """The generator refuses to emit an envelope over SEALED_MAX_BYTES. That
    constant and these caps are the same number in two files, and nothing but
    this test stops them drifting — at which point the generator would happily
    print a QR that every device silently discards."""
    from visio_schema.settings_qr.payload import SEALED_MAX_BYTES

    for field in ("SetStorage.sealed", "TestStorage.sealed",
                  "SetRecordingKey.sealed"):
        assert sizes[field] == SEALED_MAX_BYTES, (
            f"{field} max_size:{sizes[field]} != payload.SEALED_MAX_BYTES "
            f"({SEALED_MAX_BYTES})"
        )


def test_a_realistic_worst_case_envelope_fits_the_cap(
        sizes: dict[str, int]) -> None:
    """A full body — the longest secret SetStorage accepts, both recording
    keys, an expiry — must still fit, or the cap is unusably tight."""
    import json

    from visio_schema.crypto import generate_keypair, seal
    from visio_schema.settings_qr import SealedSecrets

    body = SealedSecrets(
        storage_secret="x" * (sizes["SetStorage.secret_access_key"] - 1),
        recording_key=bytes(32),
        old_recording_key=bytes(32),
        expires_at=1786000000,
    ).to_body()
    _, pub = generate_keypair()
    blob = seal(json.dumps(body, separators=(",", ":")).encode(), pub)
    assert len(blob) <= sizes["SetStorage.sealed"], (
        f"a full envelope is {len(blob)} B, over the "
        f"{sizes['SetStorage.sealed']} B cap"
    )


# All three sealed fields decode through the same C++ Unseal, and the device
# reuses one static buffer for them; a mismatch would size that buffer wrong.
def test_every_sealed_cap_agrees(sizes: dict[str, int]) -> None:
    sealed = {k: v for k, v in sizes.items() if k.endswith(".sealed")}
    assert len(sealed) == 3, f"expected three sealed fields, found {sorted(sealed)}"
    assert len(set(sealed.values())) == 1, sealed


# ── max_count, and that the generator actually applied it ────────────────────
#
# A repeated field with NO entry here degrades to `pb_callback_t`, which needs a
# hand-written encode callback the firmware does not have. A mistyped field path
# is ignored silently by the generator, so the only thing that would notice is
# the firmware cross-build failing on `sizeof(...)/sizeof(...)` — which no host
# suite in any repo runs. These two tests are what make the bound real.
_GENERATED_HEADER = (
    Path(__file__).resolve().parents[2]
    / "cpp" / "generated_nanopb" / "visio_schema" / "v1" / "sensor"
    / "system_health.pb.h"
)


def _max_counts() -> dict[str, int]:
    """Every `<full.field> max_count:N` in the file, keyed by full field path."""
    out: dict[str, int] = {}
    for line in _OPTIONS.read_text().splitlines():
        m = re.match(r"(\S+)\s+.*max_count:(\d+)", line.strip())
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def test_system_health_camera_temps_is_bounded() -> None:
    counts = _max_counts()
    field = "visio_schema.v1.sensor.SystemHealth.camera_temps"
    assert field in counts, (
        f"{field} has no max_count — the repeated field becomes pb_callback_t "
        "and the firmware cannot encode it"
    )
    # The widest board we build fits four cameras; the bound carries headroom.
    assert counts[field] >= 4


@pytest.mark.skipif(
    not _GENERATED_HEADER.exists(),
    reason="generated nanopb headers are source-only at HEAD; run `make gen`",
)
def test_camera_temps_bound_reached_the_generated_header() -> None:
    """The option is spelled correctly, so the field really is FT_STATIC.

    A wrong field path leaves the array absent and the field a callback; this
    reads the generator's own output rather than trusting the option line.
    """
    header = _GENERATED_HEADER.read_text()
    want = _max_counts()["visio_schema.v1.sensor.SystemHealth.camera_temps"]
    assert f"camera_temps[{want}]" in header
    assert "pb_size_t camera_temps_count;" in header
    assert "STATIC,   REPEATED, MESSAGE,  camera_temps" in header


# A name the listing reports must be one every recording command accepts: a
# smaller cap on any of them fails pb_decode, and the device drops the whole
# Command without answering.
def test_recording_name_caps_agree(sizes: dict[str, int]) -> None:
    fields = ("ListRecordings.cursor", "ListRecordings.session_name",
              "OpenRecordingFile.session_name", "OpenRecordingFile.file_name",
              "DeleteRecording.session_name")
    caps = {f: sizes[f] for f in fields}
    assert len(set(caps.values())) == 1, caps
