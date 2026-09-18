"""Replay tests/golden/recordings_wire_vectors.txt through the Python reference.

A REGENERATION check: it fails when the builders or the schema change without
the corpus being regenerated, which is exactly when C++ and TypeScript would
silently diverge from it.
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _golden

V = _golden.load("recordings_wire_vectors.txt")

_gen_path = Path(__file__).resolve().parents[2] / "scripts" / "gen_recordings_wire_vectors.py"
_spec = importlib.util.spec_from_file_location("gen_recordings_wire_vectors", _gen_path)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_the_corpus_and_the_generator_agree_on_the_case_set():
    expected = {f"{k}.cmd" for k in gen.COMMANDS} | {f"{k}.result" for k in gen.results()}
    assert set(V) == expected


def test_every_command_encodes_as_the_corpus_says():
    for name, raw in gen.COMMANDS.items():
        assert raw == V[f"{name}.cmd"], name


def test_every_result_encodes_as_the_corpus_says():
    for name, raw in gen.results().items():
        assert raw == V[f"{name}.result"], name


def test_the_session_files_result_decodes_with_its_writing_flags():
    from visio_schema.v1.control import command_result_pb2 as cr
    result = cr.CommandResult.FromString(V["result_session_files.result"])
    files = result.recordings.recordings[0].files
    assert [(f.name, f.writing, f.complete) for f in files] == [
        ("ego_0000.mcap", False, True),
        ("ego_0001.mcap", True, False),
        ("session.json", True, True),
    ]
