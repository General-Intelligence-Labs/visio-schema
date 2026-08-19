"""Settings-QR payload rules, sealing, and the `visio-settings-qr` CLI.

The property under test throughout is one sentence: **a v2 code never
contains a secret.** The operator who scans it is the adversary the format
exists to defend against, so "the secret is not in the bytes" is asserted
directly against the rendered payload rather than inferred from the code
path that built it.
"""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from visio_schema.settings_qr import (
    PLAINTEXT_VERSION,
    RECORDING_KEY_BYTES,
    SEALED_VERSION,
    WARN_BYTES,
    EnvelopeTooLarge,
    SealedSecrets,
    encode,
    open_sealed,
    recording_key_fingerprint,
    seal_into,
    validate,
)
from visio_schema.settings_qr.cli import main
from visio_schema.settings_qr.interactive import interactive
from visio_schema.settings_qr.payload import (
    ENDPOINT_TEMPLATES,
    normalize_storage_prefix,
    region_from_endpoint,
)

# The test fleet keypair from test_seal.py — never the shipped one.
TEST_PRIVATE = bytes(range(32))
TEST_PUBLIC = bytes.fromhex(
    "8f40c5adb68f25624ae5b214ea767a6ec94d829d3d7b5e1ad1ba6f3e2138285f"
)
SECRET = "s3cr3t-do-not-print"
RECORDING_KEY = bytes(range(100, 132))

BASE_CONFIG = {
    "t": "visio-settings",
    "v": PLAINTEXT_VERSION,
    "meta": {"task": "pick-and-place", "location": "Suzhou line 2",
             "message": "", "capturer": "operator-7"},
    "storage": {
        "endpoint_url": "https://oss-cn-hangzhou.aliyuncs.com",
        "bucket": "gilabs-captures",
        "access_key_id": "LTAI5tExampleKeyId00",
        "secret_access_key": SECRET,
        "prefix": "factory-7/",
    },
    "auto_upload": True,
}


@pytest.fixture
def config() -> dict:
    return json.loads(json.dumps(BASE_CONFIG))    # a deep copy per test


@pytest.fixture
def test_pubkey_pem(tmp_path: Path) -> str:
    """The TEST fleet public key as a file, so a CLI run's output can be
    opened with TEST_PRIVATE instead of the shipped key."""
    from visio_schema.crypto.envelope import public_key_pem

    p = tmp_path / "fleet.pem"
    p.write_bytes(public_key_pem(TEST_PUBLIC))
    return str(p)


class TestSealing:
    def test_the_secret_is_not_in_the_rendered_payload(self, config) -> None:
        """The whole point, asserted against the bytes that get printed."""
        payload = encode(seal_into(
            config, SealedSecrets(storage_secret=SECRET), pubkey=TEST_PUBLIC))
        assert SECRET not in payload

    def test_the_recording_key_is_not_in_the_rendered_payload(self, config) -> None:
        payload = encode(seal_into(
            config, SealedSecrets(recording_key=RECORDING_KEY),
            pubkey=TEST_PUBLIC))
        for encoding in (RECORDING_KEY.hex(), RECORDING_KEY.decode("latin-1")):
            assert encoding not in payload

    def test_it_strips_the_plaintext_secret_from_storage(self, config) -> None:
        out = seal_into(config, SealedSecrets(storage_secret=SECRET),
                        pubkey=TEST_PUBLIC)
        assert "secret_access_key" not in out["storage"]
        assert out["storage"]["bucket"] == "gilabs-captures"   # rest survives

    def test_it_does_not_mutate_the_caller_s_config(self, config) -> None:
        seal_into(config, SealedSecrets(storage_secret=SECRET),
                  pubkey=TEST_PUBLIC)
        assert config["storage"]["secret_access_key"] == SECRET

    def test_round_trip_through_the_device_side_open(self, config) -> None:
        out = seal_into(
            config,
            SealedSecrets(storage_secret=SECRET, recording_key=RECORDING_KEY,
                          old_recording_key=bytes(32),
                          devices=("GILABS-1a2b3c4d",), expires_at=1786000000),
            pubkey=TEST_PUBLIC)
        got = open_sealed(out, TEST_PRIVATE)
        assert got.storage_secret == SECRET
        assert got.recording_key == RECORDING_KEY
        assert got.old_recording_key == bytes(32)
        assert got.devices == ("GILABS-1a2b3c4d",)
        assert got.expires_at == 1786000000

    def test_the_discriminator_comes_first(self, config) -> None:
        """Stable key order keeps a pinned app-side fixture from churning."""
        out = seal_into(config, SealedSecrets(storage_secret=SECRET),
                        pubkey=TEST_PUBLIC)
        assert list(out)[:2] == ["t", "v"]
        assert out["v"] == SEALED_VERSION

    def test_no_secrets_means_no_envelope(self, config) -> None:
        """A bucket-only QR should not carry ~300 B that sets nothing."""
        config["storage"].pop("secret_access_key")
        out = seal_into(config, SealedSecrets(), pubkey=TEST_PUBLIC)
        assert "sealed" not in out
        assert out["v"] == SEALED_VERSION

    def test_a_realistic_payload_stays_inside_the_scan_budget(self, config) -> None:
        out = seal_into(
            config,
            SealedSecrets(storage_secret=SECRET, recording_key=RECORDING_KEY,
                          old_recording_key=bytes(32)),
            pubkey=TEST_PUBLIC)
        assert len(encode(out).encode("utf-8")) < WARN_BYTES


class TestSealedSecrets:
    def test_has_names_only_the_settings_it_carries(self) -> None:
        assert SealedSecrets(storage_secret="x").has == ["sk"]
        assert SealedSecrets(recording_key=RECORDING_KEY).has == ["rk"]
        assert SealedSecrets(storage_secret="x",
                             recording_key=RECORDING_KEY).has == ["sk", "rk"]

    def test_the_proof_key_is_never_advertised(self) -> None:
        """`rko` is proof of ownership, not a setting — the app runs no step
        for it, so listing it would make the app try to."""
        assert SealedSecrets(old_recording_key=RECORDING_KEY).has == []

    def test_clearing_is_distinct_from_leaving_alone(self) -> None:
        assert SealedSecrets(recording_key=b"").to_body()["rk"] == ""
        assert "rk" not in SealedSecrets().to_body()
        assert SealedSecrets(recording_key=b"").has == ["rk"]

    def test_a_clear_survives_the_round_trip_as_a_clear(self) -> None:
        body = SealedSecrets(recording_key=b"").to_body()
        assert SealedSecrets.from_body(body).recording_key == b""

    @pytest.mark.parametrize("bad", [b"", b"short", bytes(31), bytes(33)])
    def test_a_wrong_length_key_is_rejected_at_construction(self, bad) -> None:
        # b"" is legal for recording_key (it clears) but never for the proof.
        with pytest.raises(ValueError, match="old_recording_key"):
            SealedSecrets(old_recording_key=bad)

    @pytest.mark.parametrize("bad", [b"short", bytes(31), bytes(33)])
    def test_a_wrong_length_new_key_is_rejected(self, bad) -> None:
        with pytest.raises(ValueError, match="recording_key"):
            SealedSecrets(recording_key=bad)

    def test_fingerprint_is_the_16_hex_the_vrec_header_carries(self) -> None:
        fp = recording_key_fingerprint(RECORDING_KEY)
        assert len(fp) == 16
        assert int(fp, 16) >= 0             # lowercase hex, parses
        import hashlib
        assert fp == hashlib.sha256(RECORDING_KEY).digest()[:8].hex()


class TestValidation:
    def test_a_v2_payload_with_a_plaintext_secret_is_rejected(self, config) -> None:
        config["v"] = SEALED_VERSION
        problems = validate(config)
        assert any("secret_access_key" in p for p in problems)

    def test_v1_still_validates_with_its_secret(self, config) -> None:
        assert validate(config) == []

    def test_a_sealed_section_is_rejected_in_v1(self, config) -> None:
        config["sealed"] = {"kid": "eedd883d", "has": ["sk"], "b": "x"}
        assert any("sealed" in p for p in validate(config))

    @pytest.mark.parametrize("kid", ["", "EEDD883D", "abc", "eedd883dd"])
    def test_a_malformed_kid_is_rejected(self, config, kid) -> None:
        config["v"] = SEALED_VERSION
        config["storage"].pop("secret_access_key")
        config["sealed"] = {"kid": kid, "has": ["sk"], "b": "x"}
        assert any("kid" in p for p in validate(config))

    def test_an_unknown_has_entry_is_rejected(self, config) -> None:
        config["v"] = SEALED_VERSION
        config["storage"].pop("secret_access_key")
        config["sealed"] = {"kid": "eedd883d", "has": ["rko"], "b": "x"}
        assert any("has" in p for p in validate(config))

    def test_an_unknown_version_reports_once_and_stops(self, config) -> None:
        config["v"] = 99
        problems = validate(config)
        assert len(problems) == 1 and problems[0].startswith("v:")


class TestCli:
    def _write(self, tmp_path: Path, cfg: dict) -> str:
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg))
        return str(p)

    def test_sealing_is_the_default(self, tmp_path, config, capsys) -> None:
        rc = main(["--config", self._write(tmp_path, config), "--dry-run"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["v"] == SEALED_VERSION
        assert "sealed" in payload
        assert SECRET not in json.dumps(payload)

    def test_plaintext_is_an_opt_out_that_warns(self, tmp_path, config, capsys) -> None:
        rc = main(["--config", self._write(tmp_path, config), "--plaintext",
                   "--dry-run"])
        assert rc == 0
        out = capsys.readouterr()
        assert json.loads(out.out)["v"] == PLAINTEXT_VERSION
        assert "SECURITY" in out.err

    def test_a_recording_key_cannot_ride_in_a_plaintext_qr(
            self, tmp_path, config, capsys) -> None:
        key = tmp_path / "r.key"
        key.write_text(RECORDING_KEY.hex())
        rc = main(["--config", self._write(tmp_path, config), "--plaintext",
                   "--recording-key-file", str(key), "--first-provision",
                   "--dry-run"])
        assert rc == 2
        assert "cannot ride in a plaintext QR" in capsys.readouterr().err

    def test_rotating_without_the_previous_key_is_refused(
            self, tmp_path, config, capsys) -> None:
        key = tmp_path / "r.key"
        key.write_text(RECORDING_KEY.hex())
        rc = main(["--config", self._write(tmp_path, config),
                   "--recording-key-file", str(key), "--dry-run"])
        assert rc == 2
        assert "--old-recording-key-file" in capsys.readouterr().err

    def test_first_provision_allows_it(self, tmp_path, config, capsys) -> None:
        key = tmp_path / "r.key"
        key.write_text(RECORDING_KEY.hex())
        rc = main(["--config", self._write(tmp_path, config),
                   "--recording-key-file", str(key), "--first-provision",
                   "--dry-run"])
        assert rc == 0
        assert "rk" in json.loads(capsys.readouterr().out)["sealed"]["has"]

    def test_keygen_writes_an_unreadable_key_and_says_so(
            self, tmp_path, capsys) -> None:
        out = tmp_path / "new.key"
        assert main(["keygen", "--out", str(out)]) == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        err = capsys.readouterr().err
        assert "PERMANENTLY UNREADABLE" in err
        raw = bytes.fromhex("".join(
            line.split("#", 1)[0].strip() for line in out.read_text().splitlines()))
        assert len(raw) == RECORDING_KEY_BYTES
        assert recording_key_fingerprint(raw) in err

    def test_a_key_file_with_its_backup_banner_still_parses(
            self, tmp_path, config, capsys) -> None:
        """Every key file in secrets/ carries a banner; choking on it would
        push people toward stripping the warning."""
        key = tmp_path / "r.key"
        key.write_text("# BACK THIS UP\n#\n" + RECORDING_KEY.hex() + "\n")
        rc = main(["--config", self._write(tmp_path, config),
                   "--recording-key-file", str(key), "--first-provision",
                   "--dry-run"])
        assert rc == 0

    def test_a_truncated_key_file_is_named_not_silently_accepted(
            self, tmp_path, config, capsys) -> None:
        key = tmp_path / "r.key"
        key.write_text("deadbeef")
        rc = main(["--config", self._write(tmp_path, config),
                   "--recording-key-file", str(key), "--first-provision",
                   "--dry-run"])
        assert rc == 1
        assert "expected 32 bytes" in capsys.readouterr().err

    def test_inspect_needs_no_key_and_hides_nothing_it_can_read(
            self, tmp_path, config, capsys) -> None:
        sealed = seal_into(config, SealedSecrets(storage_secret=SECRET),
                           pubkey=TEST_PUBLIC)
        p = tmp_path / "payload.json"
        p.write_text(encode(sealed))
        assert main(["inspect", str(p)]) == 0
        out = capsys.readouterr()
        assert "gilabs-captures" in out.out      # cleartext half is shown
        assert SECRET not in out.out + out.err   # sealed half is not
        assert "sets ['sk']" in out.err

    def test_fleet_keygen_writes_a_0600_private_and_a_public_pem(
            self, tmp_path, capsys) -> None:
        priv, pub = tmp_path / "p.hex", tmp_path / "p.pem"
        assert main(["fleet-keygen", "--out-private", str(priv),
                     "--out-public", str(pub)]) == 0
        assert stat.S_IMODE(priv.stat().st_mode) == 0o600
        assert "BEGIN PUBLIC KEY" in pub.read_text()
        assert "PRIVATE KEY" not in pub.read_text()


# ---------------------------------------------------------------------------
# Payload rules carried over from the generator's original home in
# visio-embedded (tests/unit/scripts/provision/test_gen_settings_qr.py). The
# code moved; without these the schema-side copy of validate() is the untested
# one.
# ---------------------------------------------------------------------------
class TestEndpointsAndRegions:
    @pytest.mark.parametrize("endpoint,region", [
        ("https://oss-cn-hangzhou.aliyuncs.com", "cn-hangzhou"),
        ("https://oss-cn-hangzhou-internal.aliyuncs.com", "cn-hangzhou"),
        ("https://cos.ap-guangzhou.myqcloud.com", "ap-guangzhou"),
        ("https://mybucket-1250000000.cos.ap-nanjing.myqcloud.com", "ap-nanjing"),
        ("https://s3.eu-west-1.amazonaws.com", "eu-west-1"),
        ("https://s3-eu-west-1.amazonaws.com", "eu-west-1"),
        ("https://s3.amazonaws.com", "us-east-1"),      # legacy global
        ("https://minio.internal.example", ""),          # not derivable
        ("not-a-url", ""),
    ])
    def test_region_from_endpoint(self, endpoint: str, region: str) -> None:
        assert region_from_endpoint(endpoint) == region

    def test_every_provider_template_yields_a_derivable_region(self) -> None:
        """The round trip the operator relies on: pick a cloud, type a
        region, and the generator must read that same region back."""
        for template in ENDPOINT_TEMPLATES.values():
            assert region_from_endpoint(
                template.format(region="cn-hangzhou")) == "cn-hangzhou"

    def test_an_underivable_region_must_be_typed(self, config) -> None:
        config["storage"]["endpoint_url"] = "https://minio.internal.example"
        assert any("region" in p for p in validate(config))
        config["storage"]["region"] = "us-east-1"
        assert validate(config) == []


class TestStoragePrefix:
    @pytest.mark.parametrize("given,want", [
        (None, "recordings/"), ("", "recordings/"),
        ("factory-7", "factory-7/"), ("factory-7/", "factory-7/"),
    ])
    def test_normalize_storage_prefix(self, given, want) -> None:
        """The key join is prefix+name, so a missing '/' would silently glue
        the prefix onto the serial."""
        cfg = {"storage": {"prefix": given} if given is not None else {}}
        normalize_storage_prefix(cfg)
        assert cfg["storage"]["prefix"] == want

    def test_a_wrong_typed_prefix_passes_through_for_validate_to_reject(self) -> None:
        cfg = {"storage": {"prefix": 7}}
        normalize_storage_prefix(cfg)
        assert cfg["storage"]["prefix"] == 7

    def test_it_ignores_a_payload_with_no_storage_section(self) -> None:
        cfg = {"bitrate_kbps": 8000}
        normalize_storage_prefix(cfg)
        assert "storage" not in cfg


class TestFieldRules:
    @pytest.mark.parametrize("kbps,ok", [(499, False), (500, True),
                                         (50_000, True), (50_001, False)])
    def test_bitrate_range(self, config, kbps, ok) -> None:
        config["bitrate_kbps"] = kbps
        assert (validate(config) == []) is ok

    @pytest.mark.parametrize("dim,ok", [(239, False), (240, True),
                                        (4096, True), (4097, False)])
    def test_resolution_range(self, config, dim, ok) -> None:
        config["resolution"] = {"width": dim, "height": 1080}
        assert (validate(config) == []) is ok

    @pytest.mark.parametrize("psk,ok", [
        ("", True),                 # open network
        ("short", False),           # under 8
        ("hunter22", True),
        ("x" * 63, True), ("x" * 64, False),
    ])
    def test_wifi_passphrase_bounds(self, config, psk, ok) -> None:
        config["wifi"] = {"ssid": "Net", "passphrase": psk}
        assert (validate(config) == []) is ok

    def test_a_33_byte_ssid_is_rejected(self, config) -> None:
        config["wifi"] = {"ssid": "é" * 17}      # 34 utf-8 bytes
        assert any("32 bytes" in p for p in validate(config))

    def test_an_overlong_meta_field_is_rejected(self, config) -> None:
        config["meta"]["task"] = "x" * 257
        assert any("meta.task" in p for p in validate(config))

    def test_unknown_keys_are_flagged(self, config) -> None:
        config["typo"] = 1
        config["meta"]["taks"] = "x"
        problems = validate(config)
        assert any("typo" in p for p in problems)
        assert any("meta.taks" in p for p in problems)

    def test_at_least_one_section_is_required(self) -> None:
        assert any("no settings sections" in p for p in
                   validate({"t": "visio-settings", "v": PLAINTEXT_VERSION}))

    def test_a_null_section_is_rejected_like_any_other_wrong_type(
            self, config) -> None:
        """`"meta": null` used to pass while `"auto_upload": null` failed —
        one presence convention, not two."""
        config["meta"] = None
        assert validate(config) != []

    def test_encode_is_compact_and_preserves_unicode(self) -> None:
        out = encode({"t": "visio-settings", "v": 1,
                      "meta": {"location": "苏州二号线"}})
        assert ", " not in out and '": ' not in out
        assert "苏州二号线" in out          # ensure_ascii=False is deliberate


# ---------------------------------------------------------------------------
# Regressions. Each of these shipped in the first draft of this change and was
# caught in review; the failure mode is named so a future edit that reopens it
# fails with the reason attached.
# ---------------------------------------------------------------------------
class TestRegressions:
    def test_replay_guards_survive_a_secret_free_qr(self, config) -> None:
        """`--device`/`--expires-in` set no settings, so an "is it empty?"
        check based on `has` dropped the envelope entirely — handing the
        operator an unrestricted, never-expiring code they believed was
        pinned to one rig."""
        config["storage"].pop("secret_access_key")
        out = seal_into(config, SealedSecrets(devices=("GILABS-1a2b3c4d",),
                                              expires_at=1786000000),
                        pubkey=TEST_PUBLIC)
        assert "sealed" in out
        assert open_sealed(out, TEST_PRIVATE).devices == ("GILABS-1a2b3c4d",)

    def test_an_envelope_over_the_device_cap_is_refused(self, config) -> None:
        """nanopb fails pb_decode on an oversized field rather than
        truncating, so the device discards the whole Command and answers
        nothing. The 1800 B payload gate is 4.7x too loose to catch it."""
        with pytest.raises(EnvelopeTooLarge, match="decode buffer"):
            seal_into(config,
                      SealedSecrets(storage_secret=SECRET,
                                    recording_key=RECORDING_KEY,
                                    old_recording_key=bytes(32),
                                    devices=tuple(f"GILABS-abcd{i}"
                                                  for i in range(20))),
                      pubkey=TEST_PUBLIC)

    def test_resealing_a_generated_payload_is_refused(self, config) -> None:
        """A v2 payload has no plaintext secret left to collect, so re-running
        the generator over its own output silently stripped the envelope and
        printed a code that set nothing."""
        sealed = seal_into(config, SealedSecrets(storage_secret=SECRET),
                           pubkey=TEST_PUBLIC)
        with pytest.raises(ValueError, match="already carries a sealed"):
            seal_into(sealed, SealedSecrets(storage_secret=SECRET),
                      pubkey=TEST_PUBLIC)

    @pytest.mark.parametrize("rk", [0, False, [], {}])
    def test_a_falsy_non_empty_rk_is_not_a_clear(self, rk) -> None:
        """`if rk` treated 0/false/[] as "" — silently telling the device to
        STOP encrypting. This module is the spec the C++ port follows, so the
        permissiveness would have been copied."""
        with pytest.raises(ValueError, match="base64 string"):
            SealedSecrets.from_body({"rk": rk})

    def test_a_bare_string_dev_is_not_a_per_character_device_list(self) -> None:
        with pytest.raises(ValueError, match="list of serials"):
            SealedSecrets.from_body({"dev": "GILABS-1a2b3c4d"})

    @pytest.mark.parametrize("exp", ["tomorrow", 1.5, True])
    def test_a_non_integer_expiry_is_rejected(self, exp) -> None:
        with pytest.raises(ValueError, match="unix seconds"):
            SealedSecrets.from_body({"exp": exp})

    def test_non_base64_in_a_key_field_is_rejected(self) -> None:
        """b64decode discards out-of-alphabet characters instead of raising,
        so a corrupted field would decode to silently wrong bytes."""
        with pytest.raises(ValueError, match="base64"):
            SealedSecrets.from_body({"rk": "AAAA!!!!"})

    def test_an_unknown_body_field_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown fields"):
            SealedSecrets.from_body({"sk": "x", "surprise": 1})


class TestInteractive:
    """The prompt flow is host-runnable, so it gets unit tests rather than an
    exemption. It always produces the v1 shape — sealing is the caller's
    step, so exactly one place decides where a secret ends up."""

    @staticmethod
    def _script(monkeypatch, answers: list[str], secrets: list[str]) -> None:
        monkeypatch.setattr("builtins.input", lambda *_: answers.pop(0))
        monkeypatch.setattr("getpass.getpass", lambda *_: secrets.pop(0))

    def test_it_builds_a_valid_v1_config(self, monkeypatch) -> None:
        self._script(monkeypatch, [
            "n",                                   # metadata?
            "y", "Aliyun OSS", "cn-hangzhou", "gilabs-captures",
            "LTAI5tExampleKeyId00", "factory-7/", "y",   # storage + auto-upload
            "n", "n", "n",                         # bitrate, resolution, wifi
        ], [SECRET])
        cfg = interactive()
        assert cfg["v"] == PLAINTEXT_VERSION
        assert validate(cfg) == []
        assert cfg["storage"]["secret_access_key"] == SECRET
        assert cfg["storage"]["endpoint_url"] == \
            "https://oss-cn-hangzhou.aliyuncs.com"
        assert cfg["auto_upload"] is True

    def test_a_bad_number_reprompts_instead_of_discarding_the_session(
            self, monkeypatch, capsys) -> None:
        """Bailing out on a typo would throw away everything already typed,
        including getpass'd secrets."""
        self._script(monkeypatch, [
            "n", "n",
            "y", "not-a-number", "999999", "8000",   # bitrate: junk, high, ok
            "n", "n",
        ], [])
        cfg = interactive()
        assert cfg["bitrate_kbps"] == 8000
        err = capsys.readouterr().err
        assert "not a number" in err and "out of range" in err

    def test_a_custom_provider_asks_for_the_endpoint(self, monkeypatch) -> None:
        self._script(monkeypatch, [
            "n",
            "y", "custom", "us-east-1", "https://minio.internal.example",
            "b", "AKID", "recordings/", "n",
            "n", "n", "n",
        ], [SECRET])
        cfg = interactive()
        assert cfg["storage"]["endpoint_url"] == "https://minio.internal.example"


class TestCliRemainingBranches:
    def _write(self, tmp_path: Path, cfg: dict) -> str:
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg))
        return str(p)

    def test_check_only_reports_ok_and_needs_a_config(
            self, tmp_path, config, capsys) -> None:
        assert main(["--config", self._write(tmp_path, config),
                     "--check-only"]) == 0
        assert "ok" in capsys.readouterr().err
        assert main(["--check-only"]) == 2
        assert "needs --config" in capsys.readouterr().err

    def test_an_oversized_payload_is_refused_before_rendering(
            self, tmp_path, config, capsys) -> None:
        for f in ("message", "task", "location", "capturer"):
            config["meta"][f] = "x" * 255
        config["storage"]["prefix"] = "p" * 254 + "/"
        config["storage"]["bucket"] = "b" * 255
        config["storage"]["access_key_id"] = "k" * 255
        rc = main(["--config", self._write(tmp_path, config),
                   "--out", str(tmp_path / "x.png")])
        assert rc == 2
        assert "too dense to scan" in capsys.readouterr().err.replace("\n", " ")

    def test_rendering_without_qrcode_fails_with_the_install_hint(
            self, tmp_path, config, monkeypatch, capsys) -> None:
        import builtins
        real = builtins.__import__

        def no_qrcode(name, *a, **kw):
            if name == "qrcode":
                raise ImportError("no qrcode")
            return real(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", no_qrcode)
        rc = main(["--config", self._write(tmp_path, config),
                   "--out", str(tmp_path / "x.png")])
        assert rc == 1
        assert "visio-schema[qr]" in capsys.readouterr().err

    def test_it_renders_a_png_and_prints_the_fingerprint(
            self, tmp_path, config, capsys) -> None:
        pytest.importorskip("qrcode")
        key = tmp_path / "r.key"
        key.write_text(RECORDING_KEY.hex())
        out = tmp_path / "qr.png"
        rc = main(["--config", self._write(tmp_path, config),
                   "--recording-key-file", str(key), "--first-provision",
                   "--out", str(out)])
        assert rc == 0
        assert out.stat().st_size > 0
        err = capsys.readouterr().err
        assert recording_key_fingerprint(RECORDING_KEY) in err
        assert "keep every key you have ever used" in err

    def test_a_customer_fleet_key_closes_the_whole_loop(
            self, tmp_path, config, capsys) -> None:
        """fleet-keygen -> --pubkey -> open with the generated private key.
        The path a customer who will not share a key with us takes."""
        priv, pub = tmp_path / "p.hex", tmp_path / "p.pem"
        assert main(["fleet-keygen", "--out-private", str(priv),
                     "--out-public", str(pub)]) == 0
        capsys.readouterr()
        assert main(["--config", self._write(tmp_path, config),
                     "--pubkey", str(pub), "--dry-run"]) == 0
        payload = json.loads(capsys.readouterr().out)
        raw = bytes.fromhex("".join(
            line.split("#", 1)[0].strip()
            for line in priv.read_text().splitlines()))
        assert open_sealed(payload, raw).storage_secret == SECRET

    def test_a_bad_pubkey_file_is_named(self, tmp_path, config, capsys) -> None:
        bad = tmp_path / "bad.pem"
        bad.write_text("not a pem")
        rc = main(["--config", self._write(tmp_path, config),
                   "--pubkey", str(bad), "--dry-run"])
        assert rc == 1
        assert "fleet public key" in capsys.readouterr().err

    def test_expires_in_lands_in_the_envelope(
            self, tmp_path, config, capsys, test_pubkey_pem) -> None:
        before = int(time.time())
        assert main(["--config", self._write(tmp_path, config),
                     "--pubkey", test_pubkey_pem,
                     "--expires-in", "30", "--dry-run"]) == 0
        got = open_sealed(json.loads(capsys.readouterr().out), TEST_PRIVATE)
        assert before + 30 * 86400 <= got.expires_at <= before + 30 * 86400 + 5

    def test_clearing_needs_the_current_key(
            self, tmp_path, config, capsys) -> None:
        """--first-provision must NOT wave this through: clearing is the
        destructive direction and there is nothing to 'first provision'."""
        rc = main(["--config", self._write(tmp_path, config),
                   "--clear-recording-key", "--first-provision", "--dry-run"])
        assert rc == 2
        assert "clearing the recording key needs" in capsys.readouterr().err

    def test_clearing_with_proof_is_allowed(
            self, tmp_path, config, capsys, test_pubkey_pem) -> None:
        key = tmp_path / "r.key"
        key.write_text(RECORDING_KEY.hex())
        assert main(["--config", self._write(tmp_path, config),
                     "--pubkey", test_pubkey_pem,
                     "--clear-recording-key", "--old-recording-key-file",
                     str(key), "--dry-run"]) == 0
        got = open_sealed(json.loads(capsys.readouterr().out), TEST_PRIVATE)
        assert got.clears_recording_key
        assert got.old_recording_key == RECORDING_KEY

    def test_setting_and_clearing_at_once_is_rejected_by_the_parser(
            self, tmp_path, config) -> None:
        key = tmp_path / "r.key"
        key.write_text(RECORDING_KEY.hex())
        with pytest.raises(SystemExit):
            main(["--config", self._write(tmp_path, config),
                  "--recording-key-file", str(key), "--clear-recording-key",
                  "--dry-run"])

    def test_replay_guards_cannot_be_combined_with_plaintext(
            self, tmp_path, config, capsys) -> None:
        rc = main(["--config", self._write(tmp_path, config), "--plaintext",
                   "--device", "GILABS-1a2b3c4d", "--dry-run"])
        assert rc == 2
        assert "cannot be combined with --plaintext" in capsys.readouterr().err

    def test_an_unsealed_wifi_passphrase_is_called_out(
            self, tmp_path, config, capsys) -> None:
        config["wifi"] = {"ssid": "FactoryNet", "passphrase": "hunter22"}
        assert main(["--config", self._write(tmp_path, config),
                     "--dry-run"]) == 0
        assert "wifi.passphrase is NOT sealed" in capsys.readouterr().err

    def test_a_partial_meta_section_warns_that_the_rest_is_cleared(
            self, tmp_path, config, capsys) -> None:
        config["meta"] = {"task": "only-this-one"}
        assert main(["--config", self._write(tmp_path, config),
                     "--dry-run"]) == 0
        assert "will CLEAR them" in capsys.readouterr().err

    def test_a_malformed_storage_section_is_a_validation_error(
            self, tmp_path, capsys) -> None:
        cfg = {"storage": ["not", "an", "object"], "auto_upload": True}
        for extra in ([], ["--plaintext"]):
            rc = main(["--config", self._write(tmp_path, cfg), "--dry-run",
                       *extra])
            assert rc == 2, extra
            assert "Traceback" not in capsys.readouterr().err

    def test_config_that_is_not_an_object_is_named(self, tmp_path, capsys) -> None:
        p = tmp_path / "c.json"
        p.write_text("[1,2,3]")
        assert main(["--config", str(p), "--dry-run"]) == 2
        assert "must be a JSON object" in capsys.readouterr().err

    def test_invalid_json_config_is_named(self, tmp_path, capsys) -> None:
        p = tmp_path / "c.json"
        p.write_text("{oops")
        assert main(["--config", str(p), "--dry-run"]) == 2
        assert "not valid JSON" in capsys.readouterr().err

    def test_a_key_file_split_across_lines_is_refused(
            self, tmp_path, config, capsys) -> None:
        """Joining lines would splice two 16-byte halves — or two different
        keys — into one accepted 32-byte key."""
        key = tmp_path / "r.key"
        key.write_text("aa" * 16 + "\n" + "bb" * 16 + "\n")
        rc = main(["--config", self._write(tmp_path, config),
                   "--recording-key-file", str(key), "--first-provision",
                   "--dry-run"])
        assert rc == 1
        assert "exactly one hex line" in capsys.readouterr().err

    def test_keygen_refuses_to_clobber_and_force_restores_the_mode(
            self, tmp_path, capsys) -> None:
        out = tmp_path / "k.key"
        assert main(["keygen", "--out", str(out)]) == 0
        capsys.readouterr()
        assert main(["keygen", "--out", str(out)]) == 2
        assert "refusing to overwrite" in capsys.readouterr().err
        out.chmod(0o644)
        assert main(["keygen", "--out", str(out), "--force"]) == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_inspect_masks_v1_credentials_unless_asked(
            self, tmp_path, config, capsys) -> None:
        p = tmp_path / "v1.json"
        config["wifi"] = {"ssid": "Net", "passphrase": "hunter22"}
        p.write_text(encode(config))
        assert main(["inspect", str(p)]) == 0
        masked = capsys.readouterr().out
        assert SECRET not in masked and "hunter22" not in masked
        assert "redacted" in masked
        assert main(["inspect", str(p), "--reveal"]) == 0
        assert SECRET in capsys.readouterr().out

    def test_inspect_says_so_when_nothing_is_sealed(
            self, tmp_path, config, capsys) -> None:
        p = tmp_path / "v1.json"
        p.write_text(encode(config))
        assert main(["inspect", str(p)]) == 0
        assert "sealed: (none)" in capsys.readouterr().err

    def test_inspect_reads_a_long_literal_payload(self, config, capsys) -> None:
        """A literal is discriminated by shape, not by probing the
        filesystem: a 400-character path component raises ENAMETOOLONG."""
        payload = encode(seal_into(
            {"t": "visio-settings", "v": 1,
             "wifi": {"ssid": "FactoryNet", "passphrase": "hunter22"}},
            SealedSecrets(storage_secret=SECRET, recording_key=RECORDING_KEY,
                          old_recording_key=bytes(32)),
            pubkey=TEST_PUBLIC))
        assert len(payload) > 255
        assert main(["inspect", payload]) == 0
        assert "FactoryNet" in capsys.readouterr().out

    def test_inspect_reports_a_missing_file_rather_than_guessing(
            self, capsys) -> None:
        assert main(["inspect", "factory7.pngjson"]) == 1
        assert "cannot read factory7.pngjson" in capsys.readouterr().err

    def test_inspect_reads_stdin(self, config, monkeypatch, capsys) -> None:
        import io as _io
        monkeypatch.setattr("sys.stdin", _io.StringIO(encode(config)))
        assert main(["inspect", "-"]) == 0
        assert "gilabs-captures" in capsys.readouterr().out

    def test_inspect_rejects_non_payload_input(self, capsys) -> None:
        assert main(["inspect", "{not json"]) == 1
        assert "not a settings payload" in capsys.readouterr().err
