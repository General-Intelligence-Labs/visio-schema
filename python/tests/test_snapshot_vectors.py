import importlib.util
from pathlib import Path

import _golden

from visio_schema.v1.control import command_pb2 as cmd
from visio_schema.v1.control import command_result_pb2 as cr
from visio_schema.v1.service.snapshot import snapshot_pb2 as stream


def test_snapshot_reference_matches_frozen_corpus():
    path = Path(__file__).resolve().parents[2] / "scripts/gen_snapshot_vectors.py"
    spec = importlib.util.spec_from_file_location("snapshot_vectors", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.vectors() == _golden.load("snapshot_vectors.txt")


def test_one_capture_correlates_metadata_and_all_stream_chunks():
    vectors = _golden.load("snapshot_vectors.txt")
    capture = cmd.Command.FromString(vectors["capture.cmd"])
    result = cr.CommandResult.FromString(vectors["capture.result"])
    meta = result.snapshot
    assert meta.snapshot_id == result.command_id == capture.command_id
    assert meta.camera_index == 7 and (meta.width, meta.height) == (640, 360)
    assert meta.capture_pts_us == 12345678901234567
    jpeg = bytearray()
    for name in ("first.chunk", "middle.chunk", "final.chunk"):
        chunk = stream.SnapshotChunk.FromString(vectors[name])
        assert chunk.snapshot_id == meta.snapshot_id
        assert chunk.offset == len(jpeg) and 0 < len(chunk.jpeg) <= 1024
        jpeg.extend(chunk.jpeg)
    assert len(jpeg) == meta.total_bytes
    assert jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
    assert not hasattr(cmd, "ReadSnapshot") and not hasattr(cmd, "CancelSnapshot")
    assert cr.CommandResult.FromString(vectors["supported.result"]).state.snapshot_supported
    assert not cr.CommandResult.FromString(vectors["older.result"]).state.snapshot_supported
    failure = cr.CommandResult.FromString(vectors["failed.result"])
    assert not failure.ok and failure.error_code == "encode_failed"
