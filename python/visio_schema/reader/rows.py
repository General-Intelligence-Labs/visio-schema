"""rows -> elements: the row-decoding pipeline, off any row source.

A **row** is a ``(Message, Channel)`` pair — the shape `read_mcap` and `read_serial`
both yield (``reader/__init__``'s table), and the shape a Visio bus sink pops. This
module turns rows into `Element`s, so a live loop and a replay of that loop's own
recording run through **one** implementation and can be compared group for group::

    elements(read_mcap(path))            # replay
    elements(bus_rows(bus, sink))        # live

`Session` does not go through here. It reads mcap chunks with a topic filter and
merges several files by arrival before reordering, which is a different shape; what
the two genuinely share — the adapter table, the message-class resolution, the CPU
decoder binder and `_reorder` — lives in this module and `Session` imports it.

**Arrival is the message stamp, not the caller's wall clock.** `_reorder` watermarks
on the *arrival* half of an adapter's triple, and the adapters already put the
message's own stamp there (an `ImuRaw` bundle expands to samples up to ~1 s past it,
which is exactly the case arrival exists to survive). So there is no clock to inject:
``reorder_ns`` alone decides how much out-of-order lateness is tolerated before an
element is released. On a bus that makes it the bounded-lateness watermark the job
needs — cross-topic rows arrive out of order by construction, and ``reorder_ns`` must
exceed the *spread* in delivery latency across topics, not the latency itself.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import TYPE_CHECKING

from visio_schema import message_class

from ._decode import HevcDecoder
from .adapters import AdapterContext, AdapterFactory, ElementAdapter, build_adapter
from .domain import Element, Frame, FrameExposure, Ns

if TYPE_CHECKING:  # import-only: keeps this module's runtime graph thin
    from visio_schema.routing import Channel
    from visio_schema.wire.message import Message

__all__ = ["cpu_video_decoders", "elements", "resolve_message_class"]


def elements(
    rows: Iterable[tuple[Message, Channel]],
    *,
    reorder_ns: Ns = 0,
    gray: bool = False,
    adapters: Mapping[str, AdapterFactory] | None = None,
) -> Iterator[Element]:
    """Decode ``(Message, Channel)`` rows into `Element`s in ``t_ns`` order.

    ``reorder_ns`` is the lateness budget: an element is released once the arrival
    watermark has passed its ``t_ns`` by this much. ``0`` releases immediately and is
    right only for a single-topic stream or one already in order.

    ``gray=True`` decodes mono8 (VIO). There is deliberately no ``gpu``: NVDEC's deep
    pipeline delays frames relative to IMU, and a row source is by definition mixed.

    ``adapters`` overrides the global table per schema name, exactly as
    `build_adapter` documents.

    Topics pass through **verbatim** — this layer does not guess at a device prefix.
    Strip one with `strip_device_topic_prefix` in the row source if you need to.
    """
    binder = _binder(gray)
    built: dict[str, ElementAdapter] = {}

    def _adapter(channel: Channel) -> ElementAdapter:
        # Lazily, on first sight of a schema: a live stream has no index to
        # enumerate schemas from, so there is nothing to build eagerly against.
        name = channel.schema_name
        got = built.get(name)
        if got is None:
            ctx = AdapterContext(
                make_decoders=binder,
                message_class_for=lambda n: resolve_message_class(
                    n, encoding=channel.schema_encoding,
                    descriptor_set=channel.schema, where=channel.topic,
                ),
            )
            got = built[name] = build_adapter(name, ctx, overrides=adapters)
        return got

    def _triples() -> Iterator[tuple[Ns, Ns, Element]]:
        for msg, channel in rows:
            yield from _adapter(channel).emit(
                msg.payload, channel.topic, msg.timestamp.ToNanoseconds()
            )
        for adapter in built.values():
            yield from adapter.flush()

    yield from _reorder(_triples(), reorder_ns)


def resolve_message_class(
    schema_name: str, *, encoding: str, descriptor_set: bytes, where: str = ""
) -> type:
    """The generated class for ``schema_name``, else one from ``descriptor_set``.

    Generated first, always: a class built from an embedded `FileDescriptorSet` has
    identical field access but is a different type object per call site, and pinning
    the shipped one keeps a topic's elements comparable across sources. The fallback
    is what makes a self-describing schema actually pay off — a derived stream
    carrying a type this process never imported still reads.

    The descriptor bytes are passed rather than the record holding them because the
    two callers name those fields differently: an mcap ``Schema`` record spells them
    ``encoding``/``data``, a routing `Channel` spells them
    ``schema_encoding``/``schema``. ``where`` only names the source in the refusal.
    """
    try:
        return message_class(schema_name)
    except KeyError:
        pass
    if encoding != "protobuf":
        at = f" on {where!r}" if where else ""
        raise ValueError(
            f"cannot decode schema {schema_name!r}{at}: encoding {encoding!r} is not "
            "protobuf. Only protobuf sidecars are readable."
        )
    return _dynamic_message_class(schema_name, bytes(descriptor_set))


def _dynamic_message_class(name: str, data: bytes) -> type:
    """Build a message class from a schema's OWN embedded `FileDescriptorSet`.

    The pool is PRIVATE and never `descriptor_pool.Default()`. Adding a file whose
    name collides with an already-registered one raises out of the C++ pool and then
    poisons every later decode in the process — a failure that surfaces somewhere
    unrelated rather than at the channel that caused it.
    """
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    fds = descriptor_pb2.FileDescriptorSet.FromString(data)
    pool = descriptor_pool.DescriptorPool()
    return message_factory.GetMessages(list(fds.file), pool=pool)[name]


def _binder(gray: bool) -> Callable:
    pixel_format = "gray" if gray else "rgb24"
    return lambda proto: cpu_video_decoders(proto, pixel_format)


def cpu_video_decoders(
    video,
    pixel_format: str = "rgb24",
    exposure_at: Callable[[str, Ns], FrameExposure | None] | None = None,
) -> tuple[Callable, Callable]:
    """Per-camera PyAV decoders: 1-in-1-out, stamped with the current AU's ``t``.

    One decoder per topic, built on that topic's first access unit, because a
    decoder carries GOP state and two cameras interleaved on one stream would
    otherwise feed each other's reference frames.

    ``exposure_at`` is `Session`'s hook for the ``frame_info`` track; a row source
    has no such side channel and leaves it None, which is the same answer a
    recording without the (opt-in, off by default) stream gives.
    """
    decoders: dict[str, HevcDecoder] = {}
    # Resolved once, not per frame: it is fixed when the binder is built.
    at = exposure_at or (lambda _topic, _t: None)

    def emit(topic: str, t: Ns) -> Iterator[tuple[Ns, Ns, Element]]:
        dec = decoders.get(topic)
        if dec is None:
            dec = decoders[topic] = HevcDecoder(video.format, pixel_format)
        img = dec.decode(video.data)
        if img is not None:
            yield t, t, Frame(
                topic, t, img, video.frame_id, exposure=at(topic, t)
            )

    def flush() -> Iterator[tuple[Ns, Ns, Element]]:
        return
        yield  # pragma: no cover — CPU path holds no in-flight frames

    return emit, flush


def _reorder(
    items: Iterable[tuple[Ns, Ns, Element]], reorder_ns: Ns
) -> Iterator[Element]:
    """Release elements in ``t_ns`` order once safe (bounded watermark heap).

    Each item is ``(t_ns, arrival_ns, element)``. The watermark follows the
    **arrival** (the message stamp), NOT the element ``t_ns``: a single IMU bundle
    expands to samples whose ``t_ns`` reaches ~1 s past its arrival, but no *future*
    message can produce a ``t_ns`` below its own arrival, so an element is safe to
    release once the arrival watermark has passed its ``t_ns`` by ``reorder_ns``.
    Bounded: at most ~one IMU-bundle span of samples (+ a couple of frames) buffered.

    The invariant is unconditional because ``t_ns`` is never shifted off the wire
    clock (see `reader`'s module docstring). What ``reorder_ns`` buys differs by
    source: for a file it covers chunk-seam arrival overlap, for a live bus it is the
    lateness budget for cross-topic delivery jitter. An element later than the budget
    is still yielded — out of order — rather than dropped.
    """
    heap: list[tuple[Ns, int, Element]] = []
    seq = 0
    watermark: Ns = 0
    for t_ns, arrival, el in items:
        watermark = max(watermark, arrival)
        heapq.heappush(heap, (t_ns, seq, el))
        seq += 1
        cutoff = watermark - reorder_ns
        while heap and heap[0][0] <= cutoff:
            yield heapq.heappop(heap)[2]
    while heap:  # drain the tail in order
        yield heapq.heappop(heap)[2]
