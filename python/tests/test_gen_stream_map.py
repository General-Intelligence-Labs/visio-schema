"""The generated C++ tables (scripts/gen_stream_map.py) feed the embeddable
MCAP recorder, which has no descriptor pool — so the FileDescriptorSet bytes it
bakes into stream_schemas.gen.hpp are the only schema the on-device writer can
register. Nothing else exercises that generator output, so guard it here:

  1. every annotated stream's descriptor set parses and resolves its own type
     (exactly the check Foxglove performs from an MCAP protobuf schema), and
  2. the generator emits a FileDescriptorSetFor case for every annotated stream,
     so a recorded channel can never reference a schema that wasn't baked in.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from google.protobuf import descriptor_pb2, descriptor_pool

from visio_schema.wire import streams

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "gen_stream_map", _SCRIPTS / "gen_stream_map.py"
)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


def test_every_annotated_stream_descriptor_set_resolves() -> None:
    annotated = gen._annotated_streams()
    assert annotated, "no annotated StreamKinds found — codegen is broken"
    for _v, proto_type in annotated:
        data = streams.file_descriptor_set(proto_type)
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(data)  # raises on malformed bytes
        pool = descriptor_pool.DescriptorPool()
        for fdp in fds.file:
            pool.Add(fdp)
        # The on-device writer registers the schema under proto_type; this is
        # the lookup Foxglove does to decode the channel. Raises if unresolvable.
        pool.FindMessageTypeByName(proto_type)


def test_generated_schemas_cover_every_annotated_stream(tmp_path) -> None:
    gen._write_stream_schemas(tmp_path)
    text = (tmp_path / "stream_schemas.gen.hpp").read_text()
    assert "FileDescriptorSetFor" in text
    for v, _proto_type in gen._annotated_streams():
        assert f"case {v.number}:" in text, f"missing FDS case for {v.name}"
