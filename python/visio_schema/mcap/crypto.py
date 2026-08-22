"""Read a ``VREC`` recording — an MCAP encrypted at rest — as if it were plain.

The device writes ciphertext to its card and uploads it verbatim, so an
encrypted recording is unreadable by every MCAP tool that exists. This is the
way back in, and it is a first-class deliverable rather than an escape hatch:
the client admin who minted the recording key is the only person who can open
their footage, and this is what they open it with.

CONTAINER (mirrors cpp/src/mcap/recording_crypto.cc; the golden-vector test
pins the two together)::

     0  "VREC"   magic (4)
     4  fmt      u8 = 1
     5  cipher   u8 = 1 (chacha20)
     8  key_fp   (8)   SHA-256(recording_key)[:8]
    16  nonce    (12)  fresh per part
    32  ChaCha20(file_key, nonce) XOR the MCAP stream -> EOF

    file_key = HMAC-SHA256(recording_key, b"visio-rec-v1" + nonce)

SEEKABILITY IS THE POINT. ChaCha20 is a stream cipher, so plaintext byte P is
always keystream byte P: the wrapper below seeks to any offset and decrypts
from there without touching a byte before it. That is what keeps MCAP's chunk
index usable — an unseekable wrapper would silently drop `mcap` onto its
linear-scan path and turn a fast indexed read of a 2 GB recording into a full
decrypt of it.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
from pathlib import Path
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

__all__ = [
    "HEADER_BYTES",
    "MCAP_MAGIC",
    "VREC_MAGIC",
    "RecordingKeyMismatch",
    "RecordingKeyUnavailable",
    "VrecHeader",
    "fingerprint",
    "is_vrec",
    "open_recording",
    "read_vrec_header",
]

VREC_MAGIC = b"VREC"
MCAP_MAGIC = b"\x89MCAP0\r\n"
HEADER_BYTES = 32
_BLOCK = 64
_KEY_LABEL = b"visio-rec-v1"
_KEY_BYTES = 32
_NONCE_BYTES = 12

_ENV_KEY = "VISIO_RECORDING_KEY"
_ENV_KEY_FILE = "VISIO_RECORDING_KEY_FILE"
_KEYRING = Path("~/.config/visio/recording-keys.json")

class RecordingKeyUnavailable(Exception):
    """No key could be found for an encrypted recording.

    Names every source that was tried. Without this the failure surfaces as an
    opaque MCAP parse error a few hundred MB into the file, which says nothing
    about the actual problem.
    """


class RecordingKeyMismatch(Exception):
    """The key offered does not open this recording.

    Checked against the header's fingerprint BEFORE any decryption, so the
    answer is "wrong key" rather than a stream of plausible garbage. This is
    the routine case after a key rotation, when one card holds parts under two
    keys.
    """


class VrecHeader:
    """The 32 plaintext bytes at the head of an encrypted part."""

    __slots__ = ("cipher", "format", "key_fp", "nonce")

    def __init__(self, format: int, cipher: int, key_fp: bytes, nonce: bytes):
        self.format = format
        self.cipher = cipher
        self.key_fp = key_fp
        self.nonce = nonce

    @property
    def key_fp_hex(self) -> str:
        """The same 16 hex chars ``DeviceState.recording_key_fingerprint``
        reports, so "what my device says" and "what opens this file" compare as
        one string rather than two encodings of it."""
        return self.key_fp.hex()


def fingerprint(key: bytes) -> bytes:
    """``SHA-256(key)[:8]`` — the identifier a part's header carries."""
    return hashlib.sha256(key).digest()[:8]


def is_vrec(head: bytes) -> bool:
    """Cheap sniff for a reader that accepts both plaintext MCAP and VREC."""
    return head[: len(VREC_MAGIC)] == VREC_MAGIC


def read_vrec_header(head: bytes) -> VrecHeader:
    """Parse the container header, refusing anything this build cannot read."""
    if len(head) < HEADER_BYTES or not is_vrec(head):
        raise ValueError("not a VREC recording")
    fmt, cipher = head[4], head[5]
    # A future format must fail loudly rather than be decrypted with the wrong
    # cipher into convincing garbage.
    if fmt != 1 or cipher != 1:
        raise ValueError(
            f"VREC format {fmt}/cipher {cipher} is newer than this reader "
            "understands — upgrade visio-schema"
        )
    return VrecHeader(fmt, cipher, head[8:16], head[16:28])


def _derive_file_key(key: bytes, nonce: bytes) -> bytes:
    return hmac.new(key, _KEY_LABEL + nonce, hashlib.sha256).digest()


def _parse_hex_key(text: str, source: str) -> bytes:
    raw = bytes.fromhex(text.strip())
    if len(raw) != _KEY_BYTES:
        raise ValueError(
            f"{source}: a recording key is {_KEY_BYTES} bytes "
            f"({_KEY_BYTES * 2} hex chars), got {len(raw)}"
        )
    return raw


def _keys_from_environment(key_fp_hex: str) -> tuple[list[bytes], list[str]]:
    """Every key the environment offers, plus a human list of what was tried.

    The keyring is indexed by fingerprint so an admin holding several clients'
    keys just works, but the flat sources are returned too and checked against
    the header — a single-client admin should not have to know the concept.
    """
    found: list[bytes] = []
    tried: list[str] = []

    env = os.environ.get(_ENV_KEY)
    tried.append(f"${_ENV_KEY}" + ("" if env else " (unset)"))
    if env:
        found.append(_parse_hex_key(env, f"${_ENV_KEY}"))

    env_file = os.environ.get(_ENV_KEY_FILE)
    tried.append(f"${_ENV_KEY_FILE}" + ("" if env_file else " (unset)"))
    if env_file:
        path = Path(env_file).expanduser()
        found.append(_parse_hex_key(path.read_text(), str(path)))

    ring = _KEYRING.expanduser()
    if ring.is_file():
        tried.append(f"{ring} (keyed by fingerprint)")
        entry = json.loads(ring.read_text()).get(key_fp_hex)
        if entry:
            found.append(_parse_hex_key(entry, str(ring)))
    else:
        tried.append(f"{ring} (absent)")
    return found, tried


def _resolve_key(header: VrecHeader, key: bytes | str | None) -> bytes:
    want = header.key_fp
    if key is not None:
        raw = _parse_hex_key(key, "the key given") if isinstance(key, str) else key
        if fingerprint(raw) != want:
            raise RecordingKeyMismatch(
                f"this recording is encrypted under key {header.key_fp_hex}, "
                f"and the key given is {fingerprint(raw).hex()}"
            )
        return raw

    candidates, tried = _keys_from_environment(header.key_fp_hex)
    for candidate in candidates:
        if fingerprint(candidate) == want:
            return candidate
    raise RecordingKeyUnavailable(
        f"no key for recording {header.key_fp_hex}. Tried: " + "; ".join(tried)
    )


class _VrecReader(io.RawIOBase):
    """Seekable plaintext view over an encrypted part.

    Offsets are PLAINTEXT offsets throughout, so a caller that used to read a
    plain MCAP keeps its arithmetic — the container's whole design.
    """

    def __init__(self, raw: BinaryIO, header: VrecHeader, key: bytes):
        self._raw = raw
        self._header = header
        self._file_key = _derive_file_key(key, header.nonce)
        self._pos = 0
        raw.seek(0, os.SEEK_END)
        # A torn part (power cut mid-recording) is shorter than a header claims
        # nothing about — there is no length field precisely so that a torn
        # file stays readable up to its cut.
        self._size = max(0, raw.tell() - HEADER_BYTES)
        raw.seek(HEADER_BYTES)

    # -- io plumbing -------------------------------------------------------- #
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            new = offset
        elif whence == os.SEEK_CUR:
            new = self._pos + offset
        elif whence == os.SEEK_END:
            new = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if new < 0:
            raise OSError("negative seek position")
        self._pos = new
        return self._pos

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()

    # -- the decrypting read ------------------------------------------------ #
    def readinto(self, buf) -> int:
        want = len(buf)
        if want == 0 or self._pos >= self._size:
            return 0
        want = min(want, self._size - self._pos)
        self._raw.seek(HEADER_BYTES + self._pos)
        cipher_bytes = self._raw.read(want)
        if not cipher_bytes:
            return 0
        buf[: len(cipher_bytes)] = self._xor_at(self._pos, cipher_bytes)
        self._pos += len(cipher_bytes)
        return len(cipher_bytes)

    def _xor_at(self, at: int, data: bytes) -> bytes:
        """XOR `data` with the keystream starting at plaintext offset `at`.

        ChaCha20's counter addresses 64-byte blocks, so an unaligned offset is
        served by starting at the containing block and discarding the leading
        remainder — the same thing the C++ side does, and the reason a seek
        costs at most one wasted block rather than a re-read from zero.
        """
        block, within = divmod(at, _BLOCK)
        iv = block.to_bytes(4, "little") + self._header.nonce
        enc = Cipher(algorithms.ChaCha20(self._file_key, iv), mode=None).encryptor()
        if within:
            enc.update(b"\0" * within)
        return enc.update(data)


def open_recording(
    path: str | os.PathLike[str], key: bytes | str | None = None
) -> BinaryIO:
    """Open a recording, transparently decrypting a ``VREC`` one.

    A plaintext MCAP is returned as the plain file object it always was, so
    this is safe to put on every read path — nothing changes for the
    unencrypted case, and no key is consulted or required.

    Args:
        path: The ``.mcap`` file. Encrypted parts keep the same extension,
            because they are still recordings; only their bytes differ.
        key: 32 raw bytes or 64 hex chars. When omitted, resolved from
            ``$VISIO_RECORDING_KEY``, ``$VISIO_RECORDING_KEY_FILE``, then
            ``~/.config/visio/recording-keys.json`` (indexed by fingerprint,
            so an admin holding several clients' keys needs no flags).

    Returns:
        A binary, **seekable** file object over the plaintext MCAP.

    Raises:
        RecordingKeyUnavailable: Encrypted, and no key was found. Names every
            source tried.
        RecordingKeyMismatch: A key was offered but opens a different
            recording — the routine case after a rotation.
    """
    raw = open(path, "rb")
    try:
        head = raw.read(HEADER_BYTES)
        if not is_vrec(head):
            raw.seek(0)
            return raw
        header = read_vrec_header(head)
        resolved = _resolve_key(header, key)
        return io.BufferedReader(_VrecReader(raw, header, resolved))
    except Exception:
        raw.close()
        raise
