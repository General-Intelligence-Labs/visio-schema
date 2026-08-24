"""The recording-key console: minting, remembering, sealing, and opening.

visio-display is the only place the recording key is ever handled — the
capturer's app renders a badge and offers no control — so these tests stand in
for the whole write path. The device half lives in visio-embedded; what is
pinned here is that a blob this console produces is one the firmware's own
reference opener accepts, and that a key can never reach a rig without first
being on the admin's keyring.
"""
from __future__ import annotations

import base64
import json

import pytest

# Reuse the container builder that pins the C++ writer, so an "encrypted
# recording" here is byte-identical to one off a card.
from tests.test_recording_crypto import _part
from visio_schema.crypto import envelope
from visio_schema.display import recording_key as rk
from visio_schema.mcap.crypto import fingerprint, keyring_path, open_recording
from visio_schema.settings_qr.sealed_body import SEALED_MAX_BYTES, EnvelopeTooLarge, open_sealed

# the uid the device reports as DeviceInfo.serial, which is what a
# against — NOT the human "GILABS-<code8>" label a unit also carries.
SERIAL = "1a2b3c4d5e6f7080"


@pytest.fixture(autouse=True)
def _isolated_keyring(tmp_path, monkeypatch):
    """Never touch the developer's real ~/.config/visio/recording-keys.json."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VISIO_RECORDING_KEY", raising=False)
    monkeypatch.delenv("VISIO_RECORDING_KEY_FILE", raising=False)
    yield


# --------------------------------------------------------------------------
# Sealing
# --------------------------------------------------------------------------

def test_a_sealed_change_opens_with_the_fleet_private_key():
    # The device does exactly this. If it fails, every QR and every console
    # push is rejected on hardware as bad_envelope.
    change = rk.seal_key_change(rk.SET, devices=(SERIAL,))
    body = json.loads(envelope.unseal(change.blob, _repo_fleet_private()))
    assert body["rk"], "the sealed body must carry the new key"
    assert body["dev"] == [SERIAL]


#: A throwaway fleet keypair, minted once per session and substituted for the
#: shipped one by the autouse fixture below.
#:
#: These tests must NOT need the real fleet private key. It lives in the
#: superproject's `secrets/`, which a clone of this MIT repo does not have — so
#: depending on it meant 13 tests SKIPPED in public CI, silently taking the
#: sealing round-trip, the device scoping and the rotation proof with them. A
#: generated keypair exercises exactly the same code and runs anywhere.
_TEST_FLEET = envelope.generate_keypair()


@pytest.fixture(autouse=True)
def _test_fleet_key(monkeypatch):
    """Seal to the throwaway key wherever the shipped one would be used."""
    _priv, pub = _TEST_FLEET
    monkeypatch.setattr(envelope, "fleet_public_key", lambda: pub)
    yield


def _repo_fleet_private() -> bytes:
    """The private half of the keypair the fixture seals to."""
    return _TEST_FLEET[0]


def test_the_fingerprint_reported_is_the_one_the_device_will_report():
    change = rk.seal_key_change(rk.SET)
    opened = open_sealed(_payload(change.blob), _repo_fleet_private())
    assert fingerprint(opened.recording_key).hex() == change.fingerprint
    assert len(change.fingerprint) == 16


def _payload(blob: bytes) -> dict:
    return {"sealed": {"b": base64.b64encode(blob).decode(),
                       "kid": envelope.blob_key_id(blob), "has": ["rk"]}}


def test_a_change_is_scoped_to_the_named_rig_only():
    # An operator who lifts this blob off the wire must not be able to replay
    # it onto a different unit — the firmware answers wrong_device.
    change = rk.seal_key_change(rk.SET, devices=(SERIAL,))
    assert open_sealed(_payload(change.blob),
                       _repo_fleet_private()).devices == (SERIAL,)


def test_an_unscoped_change_names_no_devices():
    change = rk.seal_key_change(rk.SET)
    assert open_sealed(_payload(change.blob), _repo_fleet_private()).devices == ()


def test_a_rotation_carries_proof_of_the_current_key():
    old = rk.mint_key()
    change = rk.seal_key_change(rk.ROTATE, old_key=old.hex())
    opened = open_sealed(_payload(change.blob), _repo_fleet_private())
    assert opened.old_recording_key == old
    assert opened.recording_key not in (None, b"", old)


def test_a_clear_sets_an_empty_key_and_reports_no_fingerprint():
    old = rk.mint_key()
    change = rk.seal_key_change(rk.CLEAR, old_key=old.hex())
    opened = open_sealed(_payload(change.blob), _repo_fleet_private())
    assert opened.clears_recording_key
    assert opened.old_recording_key == old
    assert change.fingerprint == ""


def test_a_blob_stays_inside_the_devices_decode_buffer():
    # nanopb does not truncate an oversized field; it fails pb_decode and the
    # device drops the whole Command, answering nothing at all.
    change = rk.seal_key_change(rk.SET, devices=(SERIAL,))
    assert len(change.blob) <= SEALED_MAX_BYTES


def test_too_many_rigs_is_refused_rather_than_silently_undeliverable():
    with pytest.raises(EnvelopeTooLarge):
        rk.seal_key_change(rk.SET,
                           devices=tuple(f"GILABS-{i:08x}" for i in range(64)))


# --------------------------------------------------------------------------
# The ordering that protects the footage
# --------------------------------------------------------------------------

def test_the_new_key_is_on_the_keyring_before_the_blob_exists():
    change = rk.seal_key_change(rk.SET)
    ring = json.loads(keyring_path().read_text())
    assert change.fingerprint in ring, (
        "a rig must never be able to hold a key its owner does not")


def test_a_refused_change_leaves_no_key_behind():
    with pytest.raises(rk.KeyChangeError):
        rk.seal_key_change(rk.ROTATE)          # no proof of the current key
    assert not keyring_path().exists()


def test_rotating_keeps_the_previous_key_readable():
    # Parts already on the card stay encrypted under the old key, so the
    # archive is append-only: rotating is not licence to discard.
    first = rk.seal_key_change(rk.SET)
    second = rk.seal_key_change(rk.ROTATE, old_key=rk.mint_key())
    ring = json.loads(keyring_path().read_text())
    assert {first.fingerprint, second.fingerprint} <= set(ring)


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

@pytest.mark.parametrize("op", ["rotate", "clear"])
def test_replacing_or_clearing_without_the_current_key_is_refused(op):
    # The wording comes from validate_recording_key_change, shared with the
    # settings-QR generator, so both refuse the same thing for the same reason.
    with pytest.raises(rk.KeyChangeError, match="the key the rigs hold now"):
        rk.seal_key_change(op)


def test_both_ways_of_setting_a_key_enforce_one_rule():
    """A QR and this console must refuse exactly the same changes.

    They used to encode the device's `rko` requirement separately — the CLI in
    `_require_rotation_proof`, the console in its own resolver — so the two
    could drift into accepting different things, and the difference would only
    surface as `bad_old_key` on a rig in a factory.
    """
    import inspect

    from visio_schema.settings_qr import cli
    from visio_schema.settings_qr.sealed_body import (
        RecordingKeyChangeRefused,
        validate_recording_key_change,
    )
    assert "validate_recording_key_change" in inspect.getsource(
        cli._require_rotation_proof)
    assert "validate_recording_key_change" in inspect.getsource(rk._check)

    # And the rule itself: a rotation with no proof is refused, a first
    # provision without one is not.
    from visio_schema.settings_qr.sealed_body import SealedSecrets
    rotate = SealedSecrets(recording_key=b"\x01" * 32)
    with pytest.raises(RecordingKeyChangeRefused):
        validate_recording_key_change(rotate, first_provision=False)
    validate_recording_key_change(rotate, first_provision=True)


def test_setting_over_an_existing_key_points_at_rotate():
    with pytest.raises(rk.KeyChangeError, match="rotate"):
        rk.seal_key_change(rk.SET, old_key=rk.mint_key().hex())


def test_an_unknown_op_is_refused():
    with pytest.raises(rk.KeyChangeError, match="unknown op"):
        rk.seal_key_change("wipe")


def test_a_malformed_key_is_refused_before_anything_is_sealed():
    with pytest.raises(rk.KeyChangeError):
        rk.seal_key_change(rk.SET, key="not-hex")
    assert not keyring_path().exists()


def test_a_short_key_is_refused():
    with pytest.raises(rk.KeyChangeError):
        rk.seal_key_change(rk.SET, key=b"\x00" * 31)


def test_the_change_never_carries_key_material_back():
    change = rk.seal_key_change(rk.SET)
    assert not hasattr(change, "key")
    # The fingerprint is a digest, not the key: it must not be usable as one.
    assert len(bytes.fromhex(change.fingerprint)) == 8


def test_the_command_is_the_sealed_blob_verbatim():
    change = rk.seal_key_change(rk.SET)
    cmd = rk.command_for(change)
    assert cmd.WhichOneof("body") == "set_recording_key"
    assert cmd.set_recording_key.sealed == change.blob


# --------------------------------------------------------------------------
# Opening a recording from the console
# --------------------------------------------------------------------------

def _encrypted(tmp_path, key, name="ego_0000.mcap"):
    path = tmp_path / name
    path.write_bytes(_part(b"\x89MCAP0\r\n" + b"payload" * 32, key, b"n" * 12))
    return str(path)


def _serve():
    import visio_schema.display.serve as s
    return s


def test_a_key_that_opens_the_recording_is_remembered(tmp_path):
    key = rk.mint_key()
    path = _encrypted(tmp_path, key)
    _serve()._adopt_key_for(path, key.hex())
    # Remembering is the point: the next recording under this key needs no
    # key entry at all.
    with open_recording(path) as f:
        assert f.read(8) == b"\x89MCAP0\r\n"


def test_a_key_for_a_different_recording_is_rejected(tmp_path):
    path = _encrypted(tmp_path, rk.mint_key())
    from visio_schema.mcap.crypto import RecordingKeyMismatch
    with pytest.raises(RecordingKeyMismatch):
        _serve()._adopt_key_for(path, rk.mint_key().hex())
    assert not keyring_path().exists(), "a wrong key must not be remembered"


def test_offering_a_key_for_a_plaintext_recording_says_so(tmp_path):
    path = tmp_path / "plain.mcap"
    path.write_bytes(b"\x89MCAP0\r\n" + b"payload")
    with pytest.raises(ValueError, match="not encrypted"):
        _serve()._adopt_key_for(str(path), rk.mint_key().hex())


def test_an_encrypted_recording_with_no_key_is_a_prompt_not_a_failure(tmp_path):
    from visio_schema.mcap.crypto import RecordingKeyUnavailable
    key = rk.mint_key()
    path = _encrypted(tmp_path, key)
    with pytest.raises(RecordingKeyUnavailable):
        _serve()._probe_readable(path)
    # Which key is what an admin holding several clients' keys needs to know.
    assert _serve()._recording_key_fp(path) == fingerprint(key).hex()


def test_a_plaintext_recording_needs_no_key(tmp_path):
    path = tmp_path / "plain.mcap"
    path.write_bytes(b"\x89MCAP0\r\n" + b"payload")
    _serve()._probe_readable(str(path))
    assert _serve()._recording_key_fp(str(path)) is None


def test_a_recording_keyed_from_the_console_opens_with_no_key_entry(tmp_path):
    # The whole point of writing the keyring at set time: the admin who keyed
    # the rig opens its footage with nothing to re-enter.
    change = rk.seal_key_change(rk.SET)
    opened = open_sealed(_payload(change.blob), _repo_fleet_private())
    path = _encrypted(tmp_path, opened.recording_key)
    _serve()._probe_readable(path)


# --------------------------------------------------------------------------
# The endpoint the page talks to
# --------------------------------------------------------------------------

_aiohttp_test_utils = pytest.importorskip("aiohttp.test_utils")


class _StubBridge:
    """Records what the console would have put on the bus."""

    def __init__(self, serial=SERIAL):
        self.sent = []
        self._serial = serial

    def current_serial(self):
        return self._serial

    def send_command(self, cmd, **kw):
        self.sent.append(cmd)
        return {"ok": True, "state": {}}


class _StubDiscovery:
    def snapshot(self):
        return []


def _post_key(app, body):
    req = _aiohttp_test_utils.make_mocked_request("POST", "/x", app=app)

    async def _json():
        return body

    req.json = _json
    import asyncio
    return asyncio.run(_serve()._config_recording_key(req))


def _app(bridge):
    return _serve()._build_app(bridge, _StubDiscovery())


def test_the_endpoint_puts_a_sealed_blob_on_the_bus():
    bridge = _StubBridge()
    resp = _post_key(_app(bridge), {"op": "set"})
    assert resp.status == 200
    assert len(bridge.sent) == 1
    sent = bridge.sent[0]
    assert sent.WhichOneof("body") == "set_recording_key"
    opened = open_sealed(_payload(sent.set_recording_key.sealed),
                         _repo_fleet_private())
    # Scoped to the rig the bridge is actually connected to, not to whatever
    # the page claimed.
    assert opened.devices == (SERIAL,)


def test_the_reply_names_the_fingerprint_to_expect_and_no_key():
    resp = _post_key(_app(_StubBridge()), {"op": "set"})
    payload = json.loads(resp.text)
    assert len(payload["expect_fingerprint"]) == 16
    assert "key" not in payload and "rk" not in json.dumps(payload)


def test_a_rotation_recovers_the_current_key_from_the_keyring():
    # The admin keyed this rig from this machine, so they should not have to
    # paste the key back in to change it.
    first = rk.seal_key_change(rk.SET)
    bridge = _StubBridge()
    resp = _post_key(_app(bridge), {"op": "rotate",
                                    "old_fingerprint": first.fingerprint})
    assert resp.status == 200
    opened = open_sealed(_payload(bridge.sent[0].set_recording_key.sealed),
                         _repo_fleet_private())
    assert fingerprint(opened.old_recording_key).hex() == first.fingerprint


def test_a_rig_keyed_elsewhere_asks_for_the_key_rather_than_failing():
    bridge = _StubBridge()
    resp = _post_key(_app(bridge), {"op": "rotate", "old_fingerprint": "00" * 8})
    assert resp.status == 400
    assert json.loads(resp.text)["needs_current_key"] is True
    assert not bridge.sent, "nothing may reach the bus without proof"


def test_a_typed_current_key_is_accepted_when_the_keyring_has_none():
    old = rk.mint_key()
    bridge = _StubBridge()
    resp = _post_key(_app(bridge), {"op": "rotate", "old_key": old.hex()})
    assert resp.status == 200
    opened = open_sealed(_payload(bridge.sent[0].set_recording_key.sealed),
                         _repo_fleet_private())
    assert opened.old_recording_key == old


def test_a_malformed_request_never_reaches_the_bus():
    bridge = _StubBridge()
    resp = _post_key(_app(bridge), {"op": "wipe"})
    assert resp.status == 400
    assert not bridge.sent


def test_a_rig_that_has_not_announced_yet_is_keyed_unscoped():
    # Before the first DeviceInfo announce we do not know the serial. Refusing
    # would make the console useless on exactly the rigs that are slowest to
    # come up; an unscoped change is still gated by `rko`.
    bridge = _StubBridge(serial="")
    resp = _post_key(_app(bridge), {"op": "set"})
    assert resp.status == 200
    opened = open_sealed(_payload(bridge.sent[0].set_recording_key.sealed),
                         _repo_fleet_private())
    assert opened.devices == ()


# --------------------------------------------------------------------------
# Which identifier a change is scoped to
# --------------------------------------------------------------------------

def test_the_scope_is_the_serial_the_device_announces():
    """Regression: scoping to the discovery label got `wrong_device` on the
    very rig the change was built for.

    A unit has two identifiers — its uid (16 hex, DeviceInfo.serial) and the human
    `code8` that the USB product string and mDNS name carry as
    `GILABS-<code8>`. A sealed `dev` entry is checked against the uid — the
    value the device reports as DeviceInfo.serial — so the label is the wrong
    string no matter how convenient it is to reach.
    """
    bridge = _StubBridge(serial=SERIAL)
    _post_key(_app(bridge), {"op": "set"})
    opened = open_sealed(_payload(bridge.sent[0].set_recording_key.sealed),
                         _repo_fleet_private())
    assert opened.devices == (SERIAL,)
    assert not any(d.startswith("GILABS-") for d in opened.devices), (
        "the human label is not what the firmware compares")


def test_the_serial_is_taken_from_a_device_info_announce():
    from visio_schema.v1.service.device_info import device_info_pb2

    sink = _serve()._StatusSink()
    assert sink.serial() == ""
    sink.note_serial(
        device_info_pb2.DeviceInfo(device_name="ego", serial=SERIAL)
        .SerializeToString())
    assert sink.serial() == SERIAL
    # Switching devices must not leave the previous unit's serial behind, or a
    # change would be scoped to a rig that is no longer on the bus.
    sink.reset()
    assert sink.serial() == ""


def test_a_corrupt_announce_does_not_break_the_stream():
    sink = _serve()._StatusSink()
    sink.note_serial(b"\xff\xff\xff\xff not a DeviceInfo")   # must not raise
    assert sink.serial() == ""


def test_an_announce_with_no_serial_does_not_clear_a_known_one():
    from visio_schema.v1.service.device_info import device_info_pb2

    sink = _serve()._StatusSink()
    sink.note_serial(
        device_info_pb2.DeviceInfo(serial=SERIAL).SerializeToString())
    sink.note_serial(
        device_info_pb2.DeviceInfo(device_name="ego").SerializeToString())
    assert sink.serial() == SERIAL


def test_the_sink_reads_announces_from_the_frame_not_the_channel():
    """Regression: the first version keyed off the resolved channel topic.

    ChannelRegistry.resolved() ABSORBS DeviceInfo — it is a control stream, so
    no sink ever sees it — which meant the check never fired and every sealed
    change silently went out unscoped. write() must not be the thing that
    learns the serial.
    """
    from visio_schema.v1.service.device_info import device_info_pb2

    sink = _serve()._StatusSink()

    class _Ch:
        topic = "/device_info"

    class _Msg:
        payload = device_info_pb2.DeviceInfo(
            serial=SERIAL).SerializeToString()

    sink.write(_Msg(), _Ch())
    assert sink.serial() == "", (
        "write() must not learn the serial — it never sees an announce")
    assert sink.snapshot()[0] == 1     # but it is still counted for liveness


def test_status_surfaces_which_rig_is_about_to_be_keyed():
    # A manual host:port row's label is just "host:port", so without the
    # announced serial there is no way to tell which unit a key would land on.
    from visio_schema.v1.service.device_info import device_info_pb2

    s = _serve()
    mgr = object.__new__(s.BridgeManager)      # no Foxglove server needed
    mgr._lock = __import__("threading").Lock()
    mgr._current = {"id": "tcp:10.0.0.2:50001",
                    "label": "10.0.0.2:50001", "transport": "ap"}
    mgr._error = None
    mgr._ended = False
    mgr._status = s._StatusSink()
    mgr._video_sink = None

    class _Sink:                                # ws_url reads .port off this
        port = 0

    mgr._sink = _Sink()

    mgr._status.note_serial(
        device_info_pb2.DeviceInfo(serial=SERIAL).SerializeToString())
    assert mgr.status()["serial"] == SERIAL


# --------------------------------------------------------------------------
# Revealing the admin's own key
# --------------------------------------------------------------------------

def _post_reveal(app, body):
    req = _aiohttp_test_utils.make_mocked_request("POST", "/x", app=app)

    async def _json():
        return body

    req.json = _json
    import asyncio
    return asyncio.run(_serve()._reveal_recording_key(req))


def test_the_admin_can_read_back_a_key_this_machine_minted():
    # Without this the panel warns "lose this key and recordings are unreadable"
    # while offering no way to write it down.
    change = rk.seal_key_change(rk.SET)
    resp = _post_reveal(_app(_StubBridge()), {"fingerprint": change.fingerprint})
    assert resp.status == 200
    payload = json.loads(resp.text)
    assert fingerprint(bytes.fromhex(payload["key"])).hex() == change.fingerprint


def test_a_key_from_another_machine_is_reported_as_absent():
    resp = _post_reveal(_app(_StubBridge()), {"fingerprint": "00" * 8})
    assert resp.status == 404
    assert "not on this computer" in json.loads(resp.text)["error"]


def test_revealing_needs_an_explicit_fingerprint():
    # Keyed lookup, so this can never become "dump every client's key".
    resp = _post_reveal(_app(_StubBridge()), {})
    assert resp.status == 400


def test_revealing_never_asks_the_device():
    """A rig must not be able to tell its holder what it was keyed with.

    The reveal reads the keyring, so it can only show a key this machine
    already had — nothing is sent to the device and nothing comes back from it.
    """
    change = rk.seal_key_change(rk.SET)
    bridge = _StubBridge()
    _post_reveal(_app(bridge), {"fingerprint": change.fingerprint})
    assert not bridge.sent, "revealing must put nothing on the bus"


def test_setting_a_key_still_never_returns_it():
    # The reveal is the ONE endpoint that discloses key material; the setter
    # must not quietly become a second one.
    resp = _post_key(_app(_StubBridge()), {"op": "set"})
    body = json.loads(resp.text)
    assert "key" not in body
    fp = body["expect_fingerprint"]
    # Whatever is in the reply must not be the key: a fingerprint is 8 bytes.
    assert len(bytes.fromhex(fp)) == 8


def test_the_page_is_served_with_a_cache_busted_script_url():
    """A stale app.js must not be a reachable state.

    `Cache-Control: no-cache` only helps browsers that have not already stored
    a copy; an entry cached before that header existed keeps its heuristic
    freshness and is served without revalidating, which presents as new markup
    driven by old code. A URL carrying the file's mtime+size cannot be answered
    from that entry at all.
    """
    import asyncio

    s = _serve()
    app = _app(_StubBridge())
    req = _aiohttp_test_utils.make_mocked_request("GET", "/", app=app)
    resp = asyncio.run(s._index(req))
    assert 'src="/static/app.js?v=' in resp.text
    assert resp.headers["Cache-Control"] == "no-cache"


def test_the_asset_tag_changes_when_the_asset_does(tmp_path):
    s = _serve()
    f = tmp_path / "app.js"
    f.write_text("one")
    before = s._asset_tag(f)
    f.write_text("one and a bit more")          # size differs
    assert s._asset_tag(f) != before
