"""Replay tests/golden/diag_wire_vectors.txt through the Python encoder.

The reference generated the corpus, so this is a REGENERATION check: it fails
when the encoder changes without the corpus being regenerated, which is exactly
when the other languages would silently diverge from it.
"""
import sys
from pathlib import Path

from visio_schema.wire import diag

# tests/ is not a package (a sibling suite already trips over that), so reach
# the shared loader by path rather than by relative import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _golden  # noqa: E402

V = _golden.load("diag_wire_vectors.txt")
SESSION = 0x51D0

CASES = {
    "list": lambda: diag.list_message(session_id=SESSION),
    "list_addressed": lambda: diag.list_message(session_id=SESSION, target_device="GILABS-A"),
    "abort": lambda: diag.abort_message(session_id=SESSION),
    "abort_addressed": lambda: diag.abort_message(session_id=SESSION, target_device="GILABS-A"),
    "read": lambda: diag.read_message("a.vdlg", 4096, 1024, session_id=SESSION),
    "read_to_end": lambda: diag.read_message("b.vdlg", session_id=SESSION),
    "read_addressed": lambda: diag.read_message(
        "c.vdlg", 1, 2, session_id=SESSION, target_device="GILABS-B"),
}


def test_the_corpus_and_this_replay_agree_on_the_case_set():
    assert sorted(k.removesuffix(".msg") for k in V) == sorted(CASES)


def test_every_case_encodes_as_the_corpus_says():
    for name, call in CASES.items():
        assert call() == V[f"{name}.msg"], name
