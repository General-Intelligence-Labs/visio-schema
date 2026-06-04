"""io.read_frames — turn a byte stream of COBS frames into Messages.

The reusable core of a live serial reader: it buffers partial frames across chunk
boundaries and skips malformed frames (framing.md §3.2/§5).
"""
from __future__ import annotations

from visio_schema.routing import FIRST_DYNAMIC
from visio_schema.transport import read_frames
from visio_schema.wire.codec import cobs_encode
from visio_schema.wire.message import Message, encode_message


def _frame(stream_id: int, payload: bytes, seq: int = 0) -> bytes:
    msg = Message(stream_id=stream_id, payload=payload, seq=seq)
    return cobs_encode(encode_message(msg)) + b"\x00"


def test_decodes_and_buffers_across_chunk_boundaries() -> None:
    wire = _frame(FIRST_DYNAMIC, b"aa", 1) + _frame(3, b"bb", 2) + _frame(FIRST_DYNAMIC, b"cc", 3)
    chunks = [wire[i:i + 3] for i in range(0, len(wire), 3)]   # tiny chunks split frames
    out = list(read_frames(chunks))
    assert [(m.stream_id, m.payload, m.seq) for m in out] == [
        (FIRST_DYNAMIC, b"aa", 1), (3, b"bb", 2), (FIRST_DYNAMIC, b"cc", 3),
    ]


def test_skips_malformed_frame_and_continues() -> None:
    good1 = _frame(FIRST_DYNAMIC, b"ok1", 1)
    garbage = b"\x01\x02\x03\x00"          # not a valid COBS-wrapped frame
    good2 = _frame(FIRST_DYNAMIC, b"ok2", 2)
    out = list(read_frames([good1 + garbage + good2]))
    assert [m.payload for m in out] == [b"ok1", b"ok2"]


def test_empty_runs_between_delimiters_ignored() -> None:
    wire = b"\x00\x00" + _frame(FIRST_DYNAMIC, b"x", 1) + b"\x00"
    out = list(read_frames([wire]))
    assert len(out) == 1 and out[0].payload == b"x"
