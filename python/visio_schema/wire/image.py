"""VENC image envelopes shared by release tools and factory stations.

Format 1 encrypts RKFW directly. Format 3 encrypts a single gzip stream of
the SAME RKFW bytes. Length and SHA-256 always describe the expanded image.
The OTA transport and the rig package carry either envelope unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import io
import os
from pathlib import Path
import struct
import tempfile
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"VENC"
HEADER_BYTES = 56
RAW = 1
GZIP = 3  # 2 is reserved for the signed-envelope design.
MAX_IMAGE_BYTES = 512 * 1024 * 1024
BLOCK_BYTES = 64 * 1024


def rkfw_partitions(stream) -> dict[str, tuple[int, int]]:
    """Read the RKFW/RKAF table without reading partition payloads."""
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    prefix = stream.read(64)
    if len(prefix) < 64 or prefix[:4] != b"RKFW":
        raise ValueError("invalid RKFW header")
    base = struct.unpack_from("<I", prefix, 0x21)[0]
    stream.seek(base)
    header = stream.read(0x8c)
    if len(header) != 0x8c or header[:4] != b"RKAF":
        raise ValueError("invalid RKAF header")
    count = struct.unpack_from("<I", header, 0x88)[0]
    if not 0 < count <= 64:
        raise ValueError("invalid RKAF partition count")
    table = stream.read(count * 0x70)
    if len(table) != count * 0x70:
        raise ValueError("truncated RKAF partition table")
    result = {}
    for i in range(count):
        row = table[i*0x70:(i+1)*0x70]
        name = row[:32].split(b"\0", 1)[0].decode("ascii")
        pos, addr, _, length = struct.unpack_from("<4I", row, 0x60)
        if addr == 0xffffffff:
            continue
        if name in result or pos < 0x8c + count*0x70 or base+pos+length > size:
            raise ValueError("invalid RKAF partition bounds")
        result[name] = (base+pos, length)
    return result


def factory_complete(stream) -> bool:
    try:
        parts = rkfw_partitions(stream)
    except (ValueError, UnicodeError, struct.error):
        return False
    return all(name in parts for name in
               ("env", "idblock", "uboot", "misc", "boot_a", "rootfs_a",
                "oem_a", "recovery", "userdata"))


@dataclass(frozen=True)
class Header:
    format: int
    nonce: bytes
    raw_bytes: int
    raw_sha256: bytes

    @property
    def compression(self):
        return "gzip" if self.format == GZIP else "none"


def parse_header(raw: bytes, *, max_bytes=MAX_IMAGE_BYTES) -> Header:
    if len(raw) < HEADER_BYTES:
        raise ValueError("truncated VENC header")
    if raw[:4] != MAGIC:
        raise ValueError("not a VENC encrypted image")
    if raw[4] not in (RAW, GZIP):
        raise ValueError(f"unsupported VENC format {raw[4]}")
    if raw[4] == GZIP and raw[5:8] != bytes(3):
        raise ValueError("unsupported VENC compression flags")
    length = struct.unpack_from("<Q", raw, 16)[0]
    if raw[4] == GZIP and length > max_bytes:
        raise ValueError("expanded image exceeds size limit")
    return Header(raw[4], raw[8:16], length, raw[24:56])


def encode(plain: bytes, key: bytes, *, compression="none", nonce=None) -> bytes:
    if len(key) != 32:
        raise ValueError("bundle key must be 32 bytes")
    if compression not in ("none", "gzip"):
        raise ValueError(f"unsupported compression {compression}")
    if compression == "gzip" and len(plain) > MAX_IMAGE_BYTES:
        raise ValueError("expanded image exceeds size limit")
    nonce = os.urandom(8) if nonce is None else nonce
    if len(nonce) != 8:
        raise ValueError("nonce must be 8 bytes")
    fmt = GZIP if compression == "gzip" else RAW
    header = (MAGIC + bytes((fmt, 0, 0, 0)) + nonce +
              struct.pack("<Q", len(plain)) + hashlib.sha256(plain).digest())
    body = gzip.compress(plain, compresslevel=6, mtime=0) if fmt == GZIP else plain
    if fmt == GZIP:
        # Python/zlib versions differ in the gzip OS byte. Normalize it so
        # identical inputs and a fixed nonce produce the same test artifact.
        body = body[:9] + b"\xff" + body[10:]
    encryptor = Cipher(algorithms.AES(key), modes.CTR(nonce + bytes(8))).encryptor()
    return header + encryptor.update(body) + encryptor.finalize()


def decode_stream(source, dest, key: bytes, *, allow_gzip=True,
                  max_bytes=MAX_IMAGE_BYTES) -> Header:
    """Expand to a caller-owned temporary stream, with bounded output buffers.

    Callers must not publish/use the output until this function succeeds.
    """
    if len(key) != 32:
        raise ValueError("bundle key must be 32 bytes")
    prefix = bytearray()
    while len(prefix) < HEADER_BYTES:
        block = source.read(HEADER_BYTES - len(prefix))
        if not block:
            raise ValueError("truncated VENC header")
        prefix.extend(block)
    header = parse_header(prefix, max_bytes=max_bytes)
    if header.format == GZIP and not allow_gzip:
        raise ValueError("compressed images are not supported by this device")
    decryptor = Cipher(algorithms.AES(key), modes.CTR(header.nonce + bytes(8))).decryptor()
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS) if header.format == GZIP else None
    digest, produced, magic = hashlib.sha256(), 0, bytearray()

    def emit(block):
        nonlocal produced
        if produced + len(block) > header.raw_bytes:
            raise ValueError("expanded image exceeds declared length")
        digest.update(block)
        produced += len(block)
        if len(magic) < 4:
            magic.extend(block[:4-len(magic)])
        dest.write(block)

    def consume(block):
        if inflater is None:
            emit(block)
            return
        while block:
            if inflater.eof:
                raise ValueError("trailing data after gzip image")
            try:
                emit(inflater.decompress(block, BLOCK_BYTES))
            except zlib.error as exc:
                raise ValueError(f"invalid gzip image: {exc}") from exc
            block = inflater.unconsumed_tail
            if inflater.unused_data:
                raise ValueError("trailing data after gzip image")

    for block in iter(lambda: source.read(BLOCK_BYTES), b""):
        consume(decryptor.update(block))
    consume(decryptor.finalize())
    if inflater is not None and not inflater.eof:
        raise ValueError("truncated gzip image")
    if produced != header.raw_bytes:
        raise ValueError("plaintext length mismatch")
    if digest.digest() != header.raw_sha256:
        raise ValueError("sha256 mismatch — wrong key or corrupt image")
    if magic != b"RKFW":
        raise ValueError("decrypted bytes are not an RKFW update.img")
    return header


def decode(blob: bytes, key: bytes, **options) -> bytes:
    output = io.BytesIO()
    decode_stream(io.BytesIO(blob), output, key, **options)
    return output.getvalue()


def decode_file(src, dst, key: bytes, **options) -> Header:
    """Atomically publish only a fully expanded, verified factory image."""
    dst = Path(dst)
    fd, temporary = tempfile.mkstemp(prefix=dst.name + ".", suffix=".part", dir=dst.parent)
    try:
        with os.fdopen(fd, "wb") as output, open(src, "rb") as source:
            header = decode_stream(source, output, key, **options)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, dst)
        return header
    finally:
        Path(temporary).unlink(missing_ok=True)
