import hashlib
import io
import struct

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from visio_schema.wire import image

KEY = bytes(range(32))
PLAIN = b"RKFW" + bytes(range(256)) * 1000


@pytest.mark.parametrize("compression", ["none", "gzip"])
def test_image_roundtrip_in_small_reads(compression):
    blob = image.encode(PLAIN, KEY, compression=compression, nonce=bytes(8))
    class SmallReads(io.BytesIO):
        def read(self, n=-1):
            return super().read(min(n, 7))
    dest = io.BytesIO()
    h = image.decode_stream(SmallReads(blob), dest, KEY)
    assert dest.getvalue() == PLAIN
    assert h.raw_bytes == len(PLAIN)
    assert h.compression == compression


def test_legacy_encoding_is_byte_identical():
    nonce = bytes(8)
    enc = Cipher(algorithms.AES(KEY), modes.CTR(bytes(16))).encryptor()
    expected = (b"VENC\x01\0\0\0" + nonce + struct.pack("<Q", len(PLAIN)) +
                hashlib.sha256(PLAIN).digest() + enc.update(PLAIN) + enc.finalize())
    assert image.encode(PLAIN, KEY, nonce=nonce) == expected


def test_gzip_rejected_when_disabled():
    blob = image.encode(PLAIN, KEY, compression="gzip")
    with pytest.raises(ValueError, match="not supported"):
        image.decode(blob, KEY, allow_gzip=False)


@pytest.mark.parametrize("damage", ["truncate", "trailing", "corrupt", "length", "digest", "format", "limit"])
def test_invalid_images_are_rejected(damage):
    blob = bytearray(image.encode(PLAIN, KEY, compression="gzip"))
    if damage == "truncate": del blob[-8:]
    if damage == "trailing": blob += b"junk"
    if damage == "corrupt": blob[-10] ^= 1
    if damage == "length": struct.pack_into("<Q", blob, 16, 4)
    if damage == "digest": blob[24] ^= 1
    if damage == "format": blob[4] = 2
    if damage == "limit": struct.pack_into("<Q", blob, 16, image.MAX_IMAGE_BYTES + 1)
    with pytest.raises(ValueError):
        image.decode(bytes(blob), KEY)


def test_failed_decode_does_not_replace_output(tmp_path):
    src, dst = tmp_path / "release.img", tmp_path / "update.img"
    src.write_bytes(image.encode(PLAIN, KEY, compression="gzip")[:-1])
    dst.write_bytes(b"previous")
    with pytest.raises(ValueError):
        image.decode_file(src, dst, KEY)
    assert dst.read_bytes() == b"previous"
    assert not list(tmp_path.glob("*.part"))


def test_shared_golden_vectors_are_decoded_and_reproduced():
    from pathlib import Path
    fixture = Path(__file__).resolve().parents[2] / "tests/golden/ota_image_vectors.txt"
    values = dict(line.split("=", 1) for line in fixture.read_text().splitlines()
                  if line and not line.startswith("#"))
    raw = {name: bytes.fromhex(value) for name, value in values.items()}
    for name, codec in (("raw", "none"), ("gzip", "gzip")):
        assert image.decode(raw[name], raw["key"]) == raw["plain"]
        assert image.encode(raw["plain"], raw["key"], compression=codec, nonce=bytes(8)) == raw[name]
