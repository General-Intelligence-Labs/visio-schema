"""Regenerate the finite snapshot command/metadata/stream corpus."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from visio_schema.v1.control import command_pb2 as cmd
from visio_schema.v1.control import command_result_pb2 as cr
from visio_schema.v1.service.snapshot import snapshot_pb2 as stream

ID = 0x123456789ABCDEF
PTS = 12345678901234567
TARGET = "GILABS-TEST"


def vectors():
    capture = cmd.Command(command_id=ID, target_device=TARGET)
    capture.capture_snapshot.SetInParent()
    ack = cr.CommandResult(command_id=ID, ok=True)
    ack.snapshot.CopyFrom(cr.Snapshot(snapshot_id=ID, camera_index=7,
        width=640, height=360, capture_pts_us=PTS, total_bytes=2055))
    first = stream.SnapshotChunk(snapshot_id=ID, offset=0,
        jpeg=b"\xff\xd8" + bytes(range(256)) * 3 + bytes(range(254)))
    middle = stream.SnapshotChunk(snapshot_id=ID, offset=1024,
        jpeg=bytes(range(256)) * 4)
    final = stream.SnapshotChunk(snapshot_id=ID, offset=2048, jpeg=b"final\xff\xd9")
    supported = cr.CommandResult(command_id=9, ok=True)
    supported.state.snapshot_supported = True
    older = cr.CommandResult(command_id=10, ok=True)
    older.state.SetInParent()
    failed = cr.CommandResult(command_id=ID, ok=False, error_code="encode_failed",
                              error_message="Snapshot encoder failed")
    return {name: value.SerializeToString() for name, value in {
        "capture.cmd": capture, "capture.result": ack, "first.chunk": first,
        "middle.chunk": middle, "final.chunk": final, "supported.result": supported,
        "older.result": older, "failed.result": failed,
    }.items()}


if __name__ == "__main__":
    dest = Path(__file__).resolve().parents[1] / "tests/golden/snapshot_vectors.txt"
    dest.write_text("# Snapshot finite stream: one command, metadata, ordered chunks.\n" +
                    "".join(f"{name}={raw.hex()}\n" for name, raw in vectors().items()))
