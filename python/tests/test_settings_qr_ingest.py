"""Settings-QR rules for an Ingest API destination.

Two properties beyond the field rules: an ingest code never prints the device
key (it is sealed like a bucket secret), and a bucket-mode code is untouched by
any of this, so every app already in the field parses it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from _settings_qr_support import (
    TEST_PRIVATE,
    TEST_PUBLIC,
    pin_english,
    write_test_pubkey_pem,
)

from visio_schema.ingest import encode_secret
from visio_schema.settings_qr import (
    PLAINTEXT_VERSION,
    SEALED_VERSION,
    SealedSecrets,
    encode,
    open_sealed,
    seal_into,
    validate,
)
from visio_schema.settings_qr.cli import main
from visio_schema.settings_qr.ingest import BUCKET_ONLY_FIELDS
from visio_schema.settings_qr.payload import normalize_storage_prefix

DEVICE_KEY = encode_secret(bytes(range(0x40, 0x60)))

INGEST_CONFIG = {
    "t": "visio-settings",
    "v": PLAINTEXT_VERSION,
    "storage": {
        "endpoint_url": "visio+https://ingest.example.com/visio",
        "access_key_id": "rig-e54a1de5",
        "secret_access_key": DEVICE_KEY,
        "destination_hint": "site-b",
    },
}


@pytest.fixture(autouse=True)
def _english_notices(monkeypatch) -> None:
    yield from pin_english(monkeypatch)


@pytest.fixture
def config() -> dict:
    return json.loads(json.dumps(INGEST_CONFIG))


@pytest.fixture
def sealed(config) -> dict:
    return seal_into(config, SealedSecrets(storage_secret=DEVICE_KEY),
                     pubkey=TEST_PUBLIC)


def test_a_plaintext_ingest_config_is_valid(config) -> None:
    assert validate(config) == []


def test_a_plaintext_ingest_config_may_omit_the_key(config) -> None:
    del config["storage"]["secret_access_key"]
    assert validate(config) == []


def test_sealing_moves_the_device_key_into_the_envelope(sealed) -> None:
    assert sealed["v"] == SEALED_VERSION
    assert DEVICE_KEY not in encode(sealed)
    assert validate(sealed) == []
    assert open_sealed(sealed, TEST_PRIVATE).storage_secret == DEVICE_KEY


def test_a_sealed_code_refuses_a_plaintext_key(sealed) -> None:
    sealed["storage"]["secret_access_key"] = DEVICE_KEY
    assert any(e.startswith("storage.secret_access_key: not allowed in a v2")
               for e in validate(sealed))


@pytest.mark.parametrize("field", BUCKET_ONLY_FIELDS)
def test_bucket_fields_are_refused_on_an_ingest_endpoint(config, field) -> None:
    config["storage"][field] = "x"
    assert validate(config) == [f"storage.{field}: not used by an Ingest API "
                                "destination (visio+https://)"]


def test_an_unknown_field_is_refused(config) -> None:
    config["storage"]["foo"] = "x"
    assert validate(config) == ["storage.foo: unknown field"]


def test_no_prefix_is_invented_for_an_ingest_endpoint(config) -> None:
    normalize_storage_prefix(config)
    assert "prefix" not in config["storage"]


@pytest.mark.parametrize("endpoint", [
    "visio+https://", "visio+https://[", "visio+https://[oops]",
])
def test_the_endpoint_must_name_a_host(config, endpoint) -> None:
    config["storage"]["endpoint_url"] = endpoint
    assert "storage.endpoint_url: must name a host after visio+https://" in (
        validate(config))


@pytest.mark.parametrize("endpoint", ["VISIO+HTTPS://ingest.example.com",
                                      "visio+http://ingest.example.com"])
def test_only_the_exact_scheme_selects_ingest_mode(config, endpoint) -> None:
    """Anything else is a bucket endpoint, so bucket rules apply to it."""
    config["storage"]["endpoint_url"] = endpoint
    assert "storage.endpoint_url: must be an http(s) URL" in validate(config)


@pytest.mark.parametrize("key", ["too-short", 42])
def test_a_malformed_device_key_is_refused(config, key) -> None:
    config["storage"]["secret_access_key"] = key
    assert any(e.startswith("storage.secret_access_key:")
               for e in validate(config))


def test_a_padded_device_key_is_refused(config) -> None:
    """The wire form is unpadded; every port decodes it strictly."""
    config["storage"]["secret_access_key"] = DEVICE_KEY + "="
    assert any(e.startswith("storage.secret_access_key:")
               for e in validate(config))


@pytest.mark.parametrize("field", ["endpoint_url", "access_key_id"])
def test_endpoint_and_key_id_are_required(config, field) -> None:
    config["storage"][field] = ""
    assert f"storage.{field}: required" in validate(config)


def test_destination_hint_is_bounded_in_bytes(config) -> None:
    config["storage"]["destination_hint"] = "x" * 63
    assert validate(config) == []
    config["storage"]["destination_hint"] = "现" * 22     # 66 bytes, 22 chars
    assert any(e.startswith("storage.destination_hint: 66 bytes")
               for e in validate(config))


def test_destination_hint_must_be_text(config) -> None:
    config["storage"]["destination_hint"] = 7
    assert validate(config) == ["storage.destination_hint: must be a string"]


def test_a_bucket_code_carries_none_of_the_ingest_keys() -> None:
    """Older apps refuse unknown keys, so a bucket code must not grow any."""
    cfg = {"t": "visio-settings", "v": PLAINTEXT_VERSION,
           "storage": {"endpoint_url": "https://oss-cn-hangzhou.aliyuncs.com",
                       "bucket": "b", "access_key_id": "k"}}
    normalize_storage_prefix(cfg)
    assert set(cfg) == {"t", "v", "storage"}
    assert set(cfg["storage"]) == {"endpoint_url", "bucket", "access_key_id",
                                   "prefix"}
    assert validate(cfg) == []


def test_the_cli_seals_an_ingest_config(config, tmp_path: Path, capsys) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(config))
    pem = write_test_pubkey_pem(tmp_path)
    assert main(["qr", "--config", str(path), "--pubkey", pem, "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert DEVICE_KEY not in printed
    payload = json.loads(printed)
    assert payload["storage"]["endpoint_url"].startswith("visio+https://")
    assert open_sealed(payload, TEST_PRIVATE).storage_secret == DEVICE_KEY
