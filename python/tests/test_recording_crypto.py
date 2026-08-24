"""The VREC container, Python side — and the cross-language pin.

The device WRITES these bytes in C++ and the admin READS them here. They are
two separate implementations of one stream cipher, and a drift between them is
silent: recordings decrypt to convincing garbage, discovered long after the
footage was captured. ../../tests/golden/vrec_vectors.txt is the fixture that
turns that into a build failure; cpp/tests/test_recording_crypto.cc asserts the
same bytes from the other side.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from visio_schema.mcap.crypto import (
    HEADER_BYTES,
    MCAP_MAGIC,
    RecordingKeyMismatch,
    RecordingKeyUnavailable,
    fingerprint,
    is_vrec,
    open_recording,
    read_vrec_header,
)

_GOLDEN = (
    Path(__file__).resolve().parents[1].parent
    / "tests" / "golden" / "vrec_vectors.txt"
)


def _vectors() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for line in _GOLDEN.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        out[k] = bytes.fromhex(v)
    return out


V = _vectors()


def _part(plaintext: bytes, key: bytes, nonce: bytes) -> bytes:
    """Build a VREC part the way the device's writer does."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

    from visio_schema.mcap.crypto import _derive_file_key

    head = bytearray(HEADER_BYTES)
    head[0:4] = b"VREC"
    head[4], head[5] = 1, 1
    head[8:16] = fingerprint(key)
    head[16:28] = nonce
    iv = (0).to_bytes(4, "little") + nonce
    enc = Cipher(algorithms.ChaCha20(_derive_file_key(key, nonce), iv),
                 mode=None).encryptor()
    return bytes(head) + enc.update(plaintext)


# ── the cross-language pin ────────────────────────────────────────────────── #

def test_key_fingerprint_matches_the_committed_vector():
    assert fingerprint(V["vrec_key"]) == V["vrec_key_fp"]


def test_file_key_derivation_matches_the_committed_vector():
    from visio_schema.mcap.crypto import _derive_file_key
    assert _derive_file_key(V["vrec_key"], V["vrec_nonce"]) == V["vrec_file_key"]


def test_header_serialization_matches_the_committed_vector():
    header = read_vrec_header(V["vrec_header"])
    assert header.format == 1 and header.cipher == 1
    assert header.key_fp == V["vrec_key_fp"]
    assert header.nonce == V["vrec_nonce"]


def test_decrypting_the_committed_ciphertext_yields_the_committed_plaintext(tmp_path):
    part = tmp_path / "golden.mcap"
    part.write_bytes(V["vrec_header"] + V["vrec_ciphertext"])
    with open_recording(part, V["vrec_key"]) as f:
        assert f.read() == V["vrec_plaintext"]


# ── seeking, which is the container's reason for existing ─────────────────── #

@pytest.mark.parametrize("offset", [0, 1, 63, 64, 65, 100, 128, 199])
def test_reading_from_any_offset_matches_a_plain_read(tmp_path, offset):
    """A seek must cost nothing in correctness at ANY offset.

    64 is the ChaCha20 block, so 63/64/65 straddle a boundary and 1/100/199
    land mid-block — where an off-by-one in the sub-block skip lives.
    """
    plain, key, nonce = V["vrec_plaintext"], V["vrec_key"], V["vrec_nonce"]
    part = tmp_path / "seek.mcap"
    part.write_bytes(_part(plain, key, nonce))
    with open_recording(part, key) as f:
        f.seek(offset)
        assert f.read() == plain[offset:]


def test_the_reader_reports_the_plaintext_length_not_the_file_length(tmp_path):
    # Every MCAP offset is a plaintext offset; a reader that leaked the extra
    # 32 header bytes into its length would put the index out by exactly that.
    plain = V["vrec_plaintext"]
    part = tmp_path / "len.mcap"
    part.write_bytes(_part(plain, V["vrec_key"], V["vrec_nonce"]))
    assert part.stat().st_size == len(plain) + HEADER_BYTES
    with open_recording(part, V["vrec_key"]) as f:
        assert f.seek(0, os.SEEK_END) == len(plain)


def test_the_reader_is_seekable_so_mcap_uses_its_index(tmp_path):
    # Not cosmetic: `mcap` picks its fast chunk-index reader off seekable(),
    # and an unseekable wrapper silently downgrades a 2 GB recording to a full
    # linear decrypt.
    part = tmp_path / "seekable.mcap"
    part.write_bytes(_part(V["vrec_plaintext"], V["vrec_key"], V["vrec_nonce"]))
    with open_recording(part, V["vrec_key"]) as f:
        assert f.seekable()


# ── plaintext stays plaintext ─────────────────────────────────────────────── #

def test_a_plain_mcap_opens_untouched_and_needs_no_key(tmp_path, monkeypatch):
    # open_recording sits on EVERY read path, so the unencrypted case must not
    # change, must not consult a key, and must not need the crypto extra.
    monkeypatch.delenv("VISIO_RECORDING_KEY", raising=False)
    monkeypatch.delenv("VISIO_RECORDING_KEY_FILE", raising=False)
    body = MCAP_MAGIC + b"payload" + MCAP_MAGIC
    part = tmp_path / "plain.mcap"
    part.write_bytes(body)
    with open_recording(part) as f:
        assert f.read() == body


def test_is_vrec_distinguishes_the_two_containers():
    assert is_vrec(V["vrec_header"])
    assert not is_vrec(MCAP_MAGIC)


# ── the failures an admin will actually hit ───────────────────────────────── #

def test_the_wrong_key_is_named_as_such_before_any_decryption(tmp_path):
    # The routine case after a rotation: one card, parts under two keys.
    # Garbage out would be far worse than an error.
    part = tmp_path / "wrong.mcap"
    part.write_bytes(_part(V["vrec_plaintext"], V["vrec_key"], V["vrec_nonce"]))
    other = bytes(range(32, 64))
    with pytest.raises(RecordingKeyMismatch) as exc:
        open_recording(part, other)
    assert V["vrec_key_fp"].hex() in str(exc.value)


def test_no_key_anywhere_names_every_source_tried(tmp_path, monkeypatch):
    # Without this the failure surfaces as an opaque MCAP parse error a few
    # hundred MB in, saying nothing about the actual problem.
    monkeypatch.delenv("VISIO_RECORDING_KEY", raising=False)
    monkeypatch.delenv("VISIO_RECORDING_KEY_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no keyring
    part = tmp_path / "nokey.mcap"
    part.write_bytes(_part(V["vrec_plaintext"], V["vrec_key"], V["vrec_nonce"]))
    with pytest.raises(RecordingKeyUnavailable) as exc:
        open_recording(part)
    message = str(exc.value)
    assert V["vrec_key_fp"].hex() in message
    assert "VISIO_RECORDING_KEY" in message
    assert "recording-keys.json" in message


def test_the_key_is_found_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VISIO_RECORDING_KEY", V["vrec_key"].hex())
    part = tmp_path / "env.mcap"
    part.write_bytes(_part(V["vrec_plaintext"], V["vrec_key"], V["vrec_nonce"]))
    with open_recording(part) as f:
        assert f.read() == V["vrec_plaintext"]


def test_the_keyring_is_indexed_by_fingerprint(tmp_path, monkeypatch):
    # An admin holding several clients' keys should not have to say which.
    import json
    monkeypatch.delenv("VISIO_RECORDING_KEY", raising=False)
    monkeypatch.delenv("VISIO_RECORDING_KEY_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    ring = tmp_path / ".config" / "visio" / "recording-keys.json"
    ring.parent.mkdir(parents=True)
    ring.write_text(json.dumps({
        "0000000000000000": bytes(range(32, 64)).hex(),   # another client
        V["vrec_key_fp"].hex(): V["vrec_key"].hex(),
    }))
    part = tmp_path / "ring.mcap"
    part.write_bytes(_part(V["vrec_plaintext"], V["vrec_key"], V["vrec_nonce"]))
    with open_recording(part) as f:
        assert f.read() == V["vrec_plaintext"]


def test_a_future_container_version_is_refused_not_guessed_at(tmp_path):
    # Decrypting an unknown format with today's cipher would produce plausible
    # garbage; a loud refusal is the only safe answer.
    head = bytearray(V["vrec_header"])
    head[4] = 2
    with pytest.raises(ValueError, match="newer than this reader"):
        read_vrec_header(bytes(head))


def test_a_torn_part_still_reads_up_to_its_cut(tmp_path):
    # Power cut mid-recording — routine, the rig is switched off by hand. The
    # container carries no length field precisely so this stays readable.
    plain, key, nonce = V["vrec_plaintext"], V["vrec_key"], V["vrec_nonce"]
    part = tmp_path / "torn.mcap"
    part.write_bytes(_part(plain, key, nonce)[: HEADER_BYTES + 90])
    with open_recording(part, key) as f:
        assert f.read() == plain[:90]


def test_concurrent_writers_do_not_lose_each_others_keys(tmp_path, monkeypatch):
    """Two processes minting keys must not drop one another's entry.

    visio-display's console and the settings-QR generator both write this
    file. A lost update is not a cosmetic race: the keyring is the ONLY place
    a recording key exists, so an entry silently discarded is footage nobody
    can ever open again.
    """
    import multiprocessing as mp
    import os as _os

    monkeypatch.setenv("HOME", str(tmp_path))
    home = str(tmp_path)

    def writer(seed: int) -> None:
        _os.environ["HOME"] = home
        from visio_schema.mcap.crypto import remember_key
        for i in range(40):
            remember_key(bytes([seed]) * 31 + bytes([i]))

    procs = [mp.Process(target=writer, args=(s,)) for s in (0x11, 0x22, 0x33)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)

    from visio_schema.mcap.crypto import keyring_path
    entries = json.loads(keyring_path().read_text())
    assert len(entries) == 3 * 40, (
        f"expected every key to survive, got {len(entries)}")


def test_remembering_a_known_key_leaves_the_file_untouched(tmp_path, monkeypatch):
    from visio_schema.mcap.crypto import keyring_path, remember_key

    monkeypatch.setenv("HOME", str(tmp_path))
    key = b"\x5c" * 32
    remember_key(key)
    before = keyring_path().stat().st_mtime_ns
    remember_key(key)
    assert keyring_path().stat().st_mtime_ns == before
