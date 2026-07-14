"""Pin the stable public API — the curated facade plus the proto schema entry points.

The names re-exported from ``visio_schema`` (its ``__all__``) and the generated proto schema are
the contract downstream consumers depend on (see ``AGENTS.md`` and ``docs/protocol/versioning.md``).
Removing, renaming, or silently *widening* the surface is a breaking change. Submodule internals
(``visio_schema.transport.*``, ``visio_schema.wire.codec.*``, the concrete endpoint classes, …) are
deliberately NOT pinned — they may change freely.

If a public-API change is intentional, update — together — the facade ``__all__`` in
``visio_schema/__init__.py``, ``FACADE_API`` below, the ``AGENTS.md`` table, and ``CHANGELOG.md``,
and bump the version.
"""
from __future__ import annotations

import dataclasses
import inspect
import subprocess
import sys

import visio_schema

# The frozen facade: exactly these names are the stable top-level API — no more, no fewer.
FACADE_API = frozenset(
    {
        "serial_endpoint",
        "Endpoint",
        "Message",
        "ChannelRegistry",
        "Channel",
        "read_serial",
        "read_mcap",
        "McapWriter",
        "make_channel",
        "message_class",
        "command_message",
        "COMMAND",
    }
)

# Command oneof body variants — presence only, not exact equality: proto can add new commands as a
# backward-compatible MINOR change, so we assert these stay, not that nothing else appears.
REQUIRED_COMMAND_BODIES = frozenset(
    {
        "start_recording",
        "stop_recording",
        "identify",
        "set_auto_start",
        "connect_wifi",
        "scan_wifi",
        "set_storage",
        "test_storage",
        "list_recordings",
        "get_state",
        "set_calibration",
        "set_auto_upload",
        "set_notice_lang",
        "set_resolution",
    }
)


def test_facade_all_is_frozen():
    """``visio_schema.__all__`` is exactly the curated set — additions and removals both fail."""
    assert frozenset(visio_schema.__all__) == FACADE_API
    assert len(visio_schema.__all__) == len(set(visio_schema.__all__)), "duplicate in __all__"


def test_facade_names_resolve():
    """Every promised name is a real, importable attribute (not just listed in ``__all__``)."""
    missing = [name for name in FACADE_API if not hasattr(visio_schema, name)]
    assert not missing, f"names in __all__ but not importable: {missing}"


def test_facade_reexports_are_canonical():
    """Each facade name IS the submodule original, not a lookalike — important for the types that
    cross the boundary in round-trips (``read_mcap`` yields these exact classes)."""
    from visio_schema.mcap import McapWriter, read_mcap
    from visio_schema.routing import Channel, ChannelRegistry, make_channel
    from visio_schema.stream import read_serial
    from visio_schema.transport import Endpoint, serial_endpoint
    from visio_schema.wire.control import COMMAND, command_message
    from visio_schema.wire.message import Message
    from visio_schema.wire.schema import message_class

    canonical = {
        "serial_endpoint": serial_endpoint,
        "Endpoint": Endpoint,
        "Message": Message,
        "ChannelRegistry": ChannelRegistry,
        "Channel": Channel,
        "read_serial": read_serial,
        "read_mcap": read_mcap,
        "McapWriter": McapWriter,
        "make_channel": make_channel,
        "message_class": message_class,
        "command_message": command_message,
        "COMMAND": COMMAND,
    }
    assert canonical.keys() == FACADE_API, "this test drifted from FACADE_API"
    mismatched = [n for n, obj in canonical.items() if getattr(visio_schema, n) is not obj]
    assert not mismatched, f"facade re-export is not the canonical object: {mismatched}"


def test_no_internals_leaked_to_top_level():
    """Only the facade names are exposed on the package root. Any other non-module public attribute
    is an accidental re-export of internal plumbing — caught exhaustively, not via a denylist."""
    leaked = {
        name
        for name in vars(visio_schema)
        if not name.startswith("_")
        and not inspect.ismodule(getattr(visio_schema, name))
        and name not in FACADE_API
    }
    assert not leaked, f"non-facade names leaked onto visio_schema: {sorted(leaked)}"


def test_message_fields_stable():
    """The wire :class:`Message` keeps its four public fields."""
    fields = {f.name for f in dataclasses.fields(visio_schema.Message)}
    assert {"stream_id", "payload", "seq", "timestamp"} <= fields


def test_import_does_not_require_mcap():
    """Importing the package and resolving ``read_mcap`` / ``McapWriter`` must not import the
    optional ``mcap`` library — it loads lazily, only when a recording is actually read/written.
    Run in a subprocess so it's independent of whatever else the suite already imported."""
    code = (
        "import sys, visio_schema as v\n"
        "assert callable(v.read_mcap) and isinstance(v.McapWriter, type)\n"
        "leaked = [m for m in sys.modules if m == 'mcap' or m.startswith('mcap.')]\n"
        "assert not leaked, leaked\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_proto_command_schema_present():
    """The control schema a client builds: ``Command`` + its oneof bodies + ``CommandResult``.

    Message types are derived from the oneof, so the body and type checks can't drift apart.
    """
    from visio_schema.v1.control import command_pb2, command_result_pb2

    assert hasattr(command_pb2, "Command")
    body_to_type = {
        f.name: f.message_type.name
        for f in command_pb2.Command.DESCRIPTOR.oneofs_by_name["body"].fields
    }
    missing = REQUIRED_COMMAND_BODIES - body_to_type.keys()
    assert not missing, f"missing command bodies: {missing}"
    missing_types = [
        body_to_type[b]
        for b in REQUIRED_COMMAND_BODIES
        if not hasattr(command_pb2, body_to_type[b])
    ]
    assert not missing_types, f"missing command message types: {missing_types}"

    assert hasattr(command_result_pb2, "CommandResult")


def test_proto_wire_and_device_info_present():
    """The wire header + discovery types every consumer decodes."""
    from visio_schema.v1.service.device_info import device_info_pb2
    from visio_schema.v1.wire import header_pb2

    assert hasattr(header_pb2, "Header") and hasattr(header_pb2, "ControlStream")
    assert hasattr(device_info_pb2, "DeviceInfo") and hasattr(device_info_pb2, "Channel")
