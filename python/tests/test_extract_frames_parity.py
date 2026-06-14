"""Native vs pure-Python deframe parity.

The native reader compiles the SAME C++ ExtractFrames the firmware/C++ tests use;
this asserts it produces byte-identical results to the pure-Python extract_frames
on the same buffers, including the drop/partial edge cases both special-case. With
test_golden_vectors tying both to the committed cross-language bytes, green here
means native == pure-Python == C++ wire.

Skips the native leg when the `_creader` extension isn't built (pure-Python-only
installs), so the suite still passes there.
"""
from __future__ import annotations

import pytest

from visio_schema.transport.framing import extract_frames, frame_bytes
from visio_schema.wire.codec import cobs_encode, encode_frame
from visio_schema.wire.message import Message

_creader = pytest.importorskip("visio_schema._creader")


def _frame(sid: int, seq: int, ts_ns: int, payload: bytes) -> bytes:
    m = Message(stream_id=sid, payload=payload, seq=seq)
    m.timestamp.FromNanoseconds(ts_ns)
    return frame_bytes(m)


def _norm_pure(buf: bytes) -> tuple[list, int]:
    rx = bytearray(buf)
    msgs = extract_frames(rx)  # mutates rx: deletes consumed, leaves the partial
    consumed = len(buf) - len(rx)
    rows = [(m.stream_id, m.seq, m.timestamp.ToNanoseconds(), bytes(m.payload))
            for m in msgs]
    return rows, consumed


def _norm_native(buf: bytes) -> tuple[list, int]:
    frames, consumed = _creader.deframe(buf)
    rows = [(f.stream_id, f.seq, f.ts_ns, bytes(f.payload)) for f in frames]
    return rows, consumed


def _assert_parity(buf: bytes) -> tuple[list, int]:
    native = _norm_native(buf)
    pure = _norm_pure(buf)
    assert native == pure, f"native={native} pure={pure}"
    return native


def test_multiple_frames() -> None:
    buf = b"".join(_frame(16 + i, i, 1_000_000_000 + i, bytes([i]) * (i + 3))
                   for i in range(5))
    rows, consumed = _assert_parity(buf)
    assert len(rows) == 5 and consumed == len(buf)


def test_empty_and_bare_delimiters() -> None:
    # Stray 0x00s make empty runs that both sides skip without error.
    buf = b"\x00\x00" + _frame(16, 0, 1, b"x") + b"\x00"
    rows, _ = _assert_parity(buf)
    assert [r[0] for r in rows] == [16]


def test_crc_corrupt_dropped_identically() -> None:
    # Corrupt a covered byte (not the COBS structure) so the CRC fails: both
    # readers must drop the frame and still decode the following good one.
    m = Message(stream_id=16, payload=b"abcd", seq=1)
    m.timestamp.FromNanoseconds(5)
    core = bytearray(encode_frame(m.to_header(), m.payload))
    core[2] ^= 0xFF  # flips a header byte -> CRC mismatch, COBS still well-formed
    corrupt = cobs_encode(bytes(core)) + b"\x00"
    buf = corrupt + _frame(17, 2, 6, b"ok")
    rows, _ = _assert_parity(buf)
    assert [r[0] for r in rows] == [17]


def test_partial_trailing_frame_same_residual() -> None:
    full = _frame(16, 1, 9, b"hello") + _frame(17, 2, 10, b"world")
    buf = full[:-3]  # truncate the last frame (drops its 0x00 delimiter)
    rows, consumed = _assert_parity(buf)
    assert [r[0] for r in rows] == [16]   # only the first frame completes
    assert consumed < len(buf)            # the partial tail is left unconsumed
