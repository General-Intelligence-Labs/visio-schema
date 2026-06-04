"""gen_schema_blobs.py emits a C++ header of per-payload-type FileDescriptorSet
byte blobs that the embedded (nanopb) MCAP writer registers as channel schemas.
Guard it: the generator runs, every payload type's blob round-trips to a
FileDescriptorSet, and the emitted header compiles (when a host g++ is present).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GEN = _REPO / "scripts" / "gen_schema_blobs.py"
_PYPKG = _REPO / "python"

pytest.importorskip("google.protobuf")


def _run_gen(out_dir: Path) -> Path:
    env = {**os.environ, "PYTHONPATH": str(_PYPKG)}
    subprocess.run([sys.executable, str(_GEN), str(out_dir)], check=True, env=env)
    return out_dir / "visio_schema" / "wire" / "schema_blobs.gen.hpp"


def test_emitted_header_has_lookup_and_ego_types(tmp_path) -> None:
    hdr = _run_gen(tmp_path)
    text = hdr.read_text()
    assert "std::string_view FileDescriptorSetFor(" in text
    # Every type the ego firmware records must have a case.
    for fqn in ("visio_schema.sensor.v1.ImuRaw",
                "visio_schema.ros.geometry_msgs.v1.Quaternion",
                "foxglove.CompressedVideo",
                "visio_schema.sensor.v1.SystemHealth"):
        assert f'proto_type == "{fqn}"' in text, fqn


def test_each_payload_blob_roundtrips() -> None:
    """Every blob the generator embeds parses back to a FileDescriptorSet that
    actually contains its message type — the bytes Foxglove resolves against."""
    sys.path.insert(0, str(_PYPKG))
    from google.protobuf import descriptor_pb2

    from visio_schema.wire import schema

    n = 0
    for mod_name in schema._PAYLOAD_MODULES:
        mod = __import__(mod_name, fromlist=["DESCRIPTOR"])
        for desc in mod.DESCRIPTOR.message_types_by_name.values():
            data = schema.file_descriptor_set(desc.full_name)
            assert data, desc.full_name
            fds = descriptor_pb2.FileDescriptorSet()
            fds.ParseFromString(data)  # raises on malformed bytes
            all_types = {f"{f.package}.{mt.name}" if f.package else mt.name
                         for f in fds.file for mt in f.message_type}
            assert desc.full_name in all_types, desc.full_name
            n += 1
    assert n > 0


@pytest.mark.skipif(shutil.which("g++") is None, reason="no host g++")
def test_emitted_header_compiles(tmp_path) -> None:
    hdr_root = _run_gen(tmp_path).parents[2]   # the <out_dir> include root
    src = tmp_path / "check.cc"
    src.write_text(
        '#include "visio_schema/wire/schema_blobs.gen.hpp"\n'
        "int main() {\n"
        '  return visio_schema::wire::FileDescriptorSetFor('
        '"visio_schema.sensor.v1.ImuRaw").empty() ? 1 : 0;\n}\n')
    r = subprocess.run(["g++", "-std=c++17", "-fsyntax-only", f"-I{hdr_root}", str(src)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
