"""wire schema -> element(s): the registered adapter table.

`Session.stream` used to dispatch on schema name through a fixed if/elif chain, so
teaching the reader a new sensor meant editing a 1,400-line class. Every topic a
consumer outside this repo cares about — `foxglove.PoseInFrame`, `JointStates` —
fell off the end of that chain and could only arrive as an opaque `Record`.

An adapter is registered against a schema name and built ONCE per streaming pass,
because the video path owns a stateful decoder and the IMU path owns a parse
buffer. It is the write-side mirror of nothing here — it is the read half of
`ChannelRegistry`, one level up: the registry resolves bytes -> message, this
resolves message -> element(s).

Three properties of the chain it replaces are load-bearing and preserved:

* **The triple.** An adapter yields ``(sort_ns, arrival_ns, element)``, not an
  element. The two clocks differ for bundles and for NVDEC: an IMU bundle expands
  to samples up to ~1 s past its own arrival, and the reorder watermark has to
  follow arrival or the heap releases early.
* **Shared vs fresh parse buffers.** Video and IMU reuse one proto instance per
  pass; a `Record` gets a fresh one every message, because a Record escapes to the
  consumer and sits in the reorder heap, so a shared buffer would alias every
  queued one to whatever was parsed last.
* **Resolution before the first read.** Derived classes resolve when the adapter
  is built — once per file, before any of that file's messages are read, rather
  than partway through it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .domain import (
    IMAGE_SCHEMA,
    IMU_RAW_SCHEMA,
    VIDEO_SCHEMA,
    Element,
    ImuSample,
    Ns,
    Record,
)

Triple = tuple[Ns, Ns, Element]


@dataclass(frozen=True)
class AdapterContext:
    """Everything a factory needs, resolved by `Session` at stream construction.

    `make_decoders` is the backend choice (CPU PyAV vs NVDEC) already bound — a
    construction-time decision, so nothing downstream branches per frame.
    """

    make_decoders: Callable | None
    message_class_for: Callable[[str], type]


class ElementAdapter(Protocol):
    """Turns one wire message into zero or more elements."""

    def emit(self, data: bytes, topic: str, t_ns: Ns) -> Iterator[Triple]: ...

    def flush(self) -> Iterator[Triple]:
        """In-flight tail at end of chunk. Stateless adapters yield nothing."""
        ...


AdapterFactory = Callable[[str, AdapterContext], ElementAdapter]

_REGISTRY: dict[str, AdapterFactory] = {}


def element_adapter(schema_name: str) -> Callable[[AdapterFactory], AdapterFactory]:
    """Register a factory for a wire schema name."""

    def register(factory: AdapterFactory) -> AdapterFactory:
        if schema_name in _REGISTRY:
            raise ValueError(f"adapter already registered for {schema_name!r}")
        _REGISTRY[schema_name] = factory
        return factory

    return register


def registered_schemas() -> frozenset[str]:
    """Schema names with a typed adapter. Anything else falls back to `Record`."""
    return frozenset(_REGISTRY)


def build_adapter(
    schema_name: str,
    ctx: AdapterContext,
    *,
    overrides: Mapping[str, AdapterFactory] | None = None,
) -> ElementAdapter:
    """The adapter for `schema_name`, or the opaque-`Record` fallback.

    `overrides` wins over the global table. That precedence is the point: a
    consumer opts into richer typing for its own pass without retyping everyone
    else's elements. Registering a `foxglove.PoseInFrame` -> Pose adapter globally
    would break any reader that asserts it receives a `Record` — and one does.
    """
    factory = (overrides or {}).get(schema_name) or _REGISTRY.get(schema_name)
    if factory is None:
        return _RecordAdapter(schema_name, ctx)
    return factory(schema_name, ctx)


# ── decoded video / stills ─────────────────────────────────────────────── #


class _DecodedAdapter:
    """Video or stills: one shared parse buffer + a bound (emit, flush) pair.

    The buffer is per-adapter and never shared across schemas: CompressedImage
    numbers its fields differently from CompressedVideo (data=2/format=3/frame_id=4
    against frame_id=2/data=3/format=4), so parsing a JPEG into the video buffer
    does not merely mislabel it — it raises DecodeError when the payload's leading
    bytes fail the string field's UTF-8 check.
    """

    def __init__(self, schema_name: str, ctx: AdapterContext) -> None:
        if ctx.make_decoders is None:
            raise ValueError(
                f"{schema_name} needs a decode backend; this is a raw pass"
            )
        self._proto = ctx.message_class_for(schema_name)()
        self._emit, self._flush = ctx.make_decoders(self._proto)

    def emit(self, data: bytes, topic: str, t_ns: Ns) -> Iterator[Triple]:
        self._proto.ParseFromString(data)
        yield from self._emit(topic, t_ns)

    def flush(self) -> Iterator[Triple]:
        yield from self._flush()


# ── bundled IMU ────────────────────────────────────────────────────────── #


class _ImuAdapter:
    """One `ImuRaw` bundle -> N `ImuSample`, each on its own sample clock.

    The 1->N expansion is why an adapter yields an iterator rather than an
    element, and why the triple carries two clocks: the samples' own times sort
    them, the bundle's arrival advances the reorder watermark.
    """

    def __init__(self, schema_name: str, ctx: AdapterContext) -> None:
        self._proto = ctx.message_class_for(schema_name)()

    def emit(self, data: bytes, topic: str, t_ns: Ns) -> Iterator[Triple]:
        self._proto.ParseFromString(data)
        for sample_ns, sample in _imu_samples(topic, t_ns, self._proto):
            yield sample_ns, t_ns, sample

    def flush(self) -> Iterator[Triple]:
        return iter(())  # nothing is held between bundles


def _imu_samples(topic: str, anchor_ns: Ns, m) -> Iterator[tuple[Ns, ImuSample]]:
    """Unbundle an ImuRaw onto the wire clock (anchor = header ts).

    The producer contract is ``Header.timestamp == ImuRaw.first_sample_time``
    before receive-side rewriting, so the (possibly NTP-rewritten) header IS the
    aligned first-sample time and ``anchor + t_offset_ns`` is the absolute sample
    time. This is decoding the bundle format, not correcting a clock — the
    result is on the same wire clock as every camera frame.
    """
    for s in m.samples:
        t = anchor_ns + int(s.t_offset_ns)
        a, g = s.linear_acceleration, s.angular_velocity
        mag = None
        if s.HasField("magnetic_field"):
            mf = s.magnetic_field
            mag = np.array([mf.x, mf.y, mf.z], float)
        yield t, ImuSample(
            topic=topic,
            t_ns=t,
            gyro=np.array([g.x, g.y, g.z], float),
            accel=np.array([a.x, a.y, a.z], float),
            mag=mag,
        )


# ── everything else: opaque, but on the same clock ─────────────────────── #


class _RecordAdapter:
    """Any other schema -> `Record`, parsed but not interpreted.

    A FRESH message instance per call, unlike the buffers above: a Record escapes
    to the consumer and sits in the reorder heap for up to `reorder_ns`, so a
    shared buffer would alias every queued one to whatever was parsed last.
    """

    def __init__(self, schema_name: str, ctx: AdapterContext) -> None:
        self._schema_name = schema_name
        # Resolved HERE, at build time, so an unresolvable schema fails at open
        # rather than an hour into a pass.
        self._cls = ctx.message_class_for(schema_name)

    def emit(self, data: bytes, topic: str, t_ns: Ns) -> Iterator[Triple]:
        proto = self._cls()
        proto.ParseFromString(data)
        yield t_ns, t_ns, Record(topic, t_ns, self._schema_name, proto)

    def flush(self) -> Iterator[Triple]:
        return iter(())  # stateless


# The built-ins, in one readable block. `Session._build_adapters` constructs video
# before stills so `flush` drains them in that order — the order the if/elif chain
# this replaced flushed in, and the one the NVDEC tail depends on. That ordering is
# enforced there, not by this table.
element_adapter(VIDEO_SCHEMA)(_DecodedAdapter)
element_adapter(IMAGE_SCHEMA)(_DecodedAdapter)
element_adapter(IMU_RAW_SCHEMA)(_ImuAdapter)
