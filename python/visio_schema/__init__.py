"""visio_schema — the Visio wire contract: generated protobuf bindings + the framing codec.

This top level is the **stable public API**. Import the names below from the package root::

    from visio_schema import read_serial, read_mcap, Message, COMMAND, command_message

and reach the message types through the generated schema packages
(``visio_schema.v1.*`` and ``visio_schema.foxglove.*``), e.g.
``from visio_schema.v1.control import command_pb2``.

Stability contract: the names in ``__all__`` here, plus the generated proto schema, are what
downstream consumers may depend on. Changing or removing one is a breaking change requiring a
MAJOR version bump (see ``docs/protocol/versioning.md``); the surface is pinned by
``python/tests/test_public_api.py``. Everything reachable through the submodules
(``visio_schema.transport.*``, ``visio_schema.wire.codec.*``, the concrete endpoint classes, the
registry internals, …) is **advanced/internal**: still importable, but not covered by this
guarantee and may change without a major bump.

The three things most users do — see ``docs/usage.md`` and ``examples/`` for runnable recipes:

* **Live view** a device — `read_serial` yields resolved ``(message, channel)`` rows.
* **Read a recording** — `read_mcap` yields the same rows; `message_class` decodes a payload,
  `McapWriter` + `make_channel` write one.
* **Integrate + command** — open a bidirectional `serial_endpoint`, then send a
  ``command_pb2.Command`` with `command_message` (wraps it onto the `COMMAND` stream).

``read_mcap`` / `McapWriter` use the ``mcap`` dependency (installed by default,
imported lazily); importing this package never imports ``mcap``, and a clear error is
raised only if you read or write a recording in an environment missing it.
"""
from importlib import import_module as _import_module
from typing import TYPE_CHECKING as _TYPE_CHECKING

if _TYPE_CHECKING:
    from visio_schema.mcap import McapWriter, read_mcap
    from visio_schema.routing import Channel, ChannelRegistry, make_channel
    from visio_schema.stream import read_serial
    from visio_schema.transport import Endpoint, serial_endpoint
    from visio_schema.wire.control import COMMAND, command_message
    from visio_schema.wire.message import Message
    from visio_schema.wire.schema import message_class

__all__ = [
    "COMMAND",
    "Channel",
    "ChannelRegistry",
    "Endpoint",
    "McapWriter",
    "Message",
    "command_message",
    "make_channel",
    "message_class",
    "read_mcap",
    "read_serial",
    "serial_endpoint",
]

# Crypto/QR consumers do not need generated protobuf bindings. Keep the same
# facade objects, but load their modules only when a facade name is requested.
_MODULES = {
    "McapWriter": "mcap", "read_mcap": "mcap",
    "Channel": "routing", "ChannelRegistry": "routing", "make_channel": "routing",
    "read_serial": "stream", "Endpoint": "transport", "serial_endpoint": "transport",
    "COMMAND": "wire.control", "command_message": "wire.control",
    "Message": "wire.message", "message_class": "wire.schema",
}


def __getattr__(name: str):
    if name not in _MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(_import_module(f".{_MODULES[name]}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
