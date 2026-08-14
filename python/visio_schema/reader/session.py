"""Layer 0 — ``Session``: a streaming, bounded-memory source over a Visio
recording (alignment §8.2).

Three access paths:
  * **cheap indexed metadata** (``topics()`` / ``calibration`` / ``metadata``) —
    read from the MCAP summary/index + a targeted seek of the small calibration
    topics, never a full decode pass;
  * the **single forward streaming pass** (``stream``) — one bounded-memory
    generator of ``Frame | ImuSample | Record`` in capture-time order across the
    union of the recording's chunks **and any sidecars**, with the device topic
    prefix stripped inline and a small reorder buffer smoothing chunk seams / IMU
    bundle spread. Naming a derived (sidecar) topic yields its messages as
    ``Record``s interleaved with the frames on the same clock, so a downstream
    stage reads an upstream stage's output exactly like a native stream and
    ``ops.sync`` matches the two by timestamp;
  * the **sparse video pass** (``keyframe_cadence`` + ``keyframe_stream``) — one
    camera topic, keyframes only, so the P-frames between samples are never
    decoded. A separate path rather than a mode of ``stream``: it is one topic and
    one decoder with no merge, no reorder and no ``Element`` union, and its whole
    value is the work it skips.

**The clock.** The wire stamp is the heartbeat-synchronized ``Header.timestamp``,
which ``McapWriter`` stores as the MCAP ``log_time`` and ``read_mcap`` restores
verbatim — so ``log_time`` is the NTP-corrected value, and it is the *only* place
that correction lives (the bus rewrites the header; the protobuf payload is opaque
to it and keeps device-local time). The producer contract is
``Header.timestamp == ImuRaw.first_sample_time`` *before* receive-side rewriting,
which is why ``log_time + t_offset_ns`` is the correct absolute sample time.

``Element.t_ns`` **is** that wire stamp, unmodified, for every element kind.
``Session`` applies no sensor-latency or exposure correction — see the
``domain`` module docstring for why that is the SDK's job to *inform*, not to
decide. Unbundling an ``ImuRaw`` into per-sample times is decoding the wire
format, not correcting it, so that stays.
"""

from __future__ import annotations

import bisect
import heapq
import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import replace
from functools import cache
from pathlib import Path

import numpy as np
from mcap.reader import make_reader

from visio_schema import message_class

from ._decode import KEYFRAME_FORMATS, HevcDecoder, is_keyframe
from .adapters import AdapterContext, AdapterFactory, build_adapter
from .domain import (
    CAM_CALIB_SCHEMA,
    FRAME_INFO_SCHEMA,
    FRAME_TF_SCHEMA,
    IMAGE_SCHEMA,
    IMU_CALIB_SCHEMA,
    IMU_RAW_SCHEMA,
    VIDEO_SCHEMA,
    Calibration,
    CameraCalib,
    Element,
    Frame,
    FrameExposure,
    KeyframeCadence,
    Ns,
    Record,
    SessionMeta,
    TopicInfo,
    make_T,
)

FRAME_INFO_SUFFIX = "/frame_info"
CAPTURE_METADATA = "visio.capture"
_CALIB_SCHEMAS = frozenset({CAM_CALIB_SCHEMA, FRAME_TF_SCHEMA, IMU_CALIB_SCHEMA})
# Calibration rides on these suffixes; `/tf` shares FrameTransform's schema but is
# a runtime pose stream, so schema alone would drag it in — see `_calib_topics`.
_CALIB_SUFFIXES = ("/intrinsics", "/extrinsics", "/info")
_log = logging.getLogger("visio_schema.reader.session")

# Above this share of the shorter file's span, an "overlap" is not a chunk seam —
# it is two parallel streams flattened into one list (see `Session._seam_slack`).
# Real seams measure well under 1% (985 ms against ~132 s chunks).
_PARALLEL_OVERLAP_FRAC = 0.5

# Hard ceiling on the auto-widened window, whatever the seams measure. The
# relative guard above only catches the pathological case; between a real 985 ms
# seam and a refused 50% overlap lies a wide band that would be honoured
# silently, and the heap holds one window of ELEMENTS. In decoded mode that is
# ~372 MB per second of window (two 1080p cams), and with `gpu=True` it is
# device-resident. 5 s is ~5x the worst seam ever measured on this hardware.
_MAX_SEAM_SLACK_NS = 5_000_000_000

# What a BARE `stream()` selects. Deliberately excludes IMAGE_SCHEMA: naming a topic
# is how a derived stream opts in (`_wanted_prefixed`), and "no topics -> the
# recording's own video and IMU" is a documented guarantee (docs/output.md).
_DECODABLE = frozenset({VIDEO_SCHEMA, IMU_RAW_SCHEMA})
# What `_iter_file` decodes natively rather than parsing into a `Record`. Wider
# than `_DECODABLE` so a NAMED image topic yields a `Frame` without joining the
# default selection.
_DECODED = _DECODABLE | {IMAGE_SCHEMA}

# `keyframe_cadence`'s probe budget. Five keyframes give four spacings to take a
# median of, which on a real recording is ~150 access units — well inside the
# first mcap chunk, so the probe costs one chunk decompression and no decode at
# all. The AU cap (the read is topic-filtered, so it counts only this camera's) is
# the escape for a stream whose keyframes are very far apart, or absent: the probe
# gives up rather than reading a whole file to find out.
_CADENCE_KEYFRAMES = 5
_CADENCE_AUS = 3000


def strip_device_topic_prefix(topic: str, device: str | None) -> str | None:
    """Remove a ``/<device>`` prefix; None only for a DIFFERENT device's topic.

    Native multi-device recordings prefix every topic with the device name
    (``/GILABS-AABBCCDD/ego/camera/0``). ``device=None`` passes topics through.

    A topic carrying no ``/GILABS-*`` component at all is **already canonical** and
    passes through unchanged — that is what `SidecarWriter` writes, and a session
    that unions a sidecar into a device-prefixed recording has both forms at once.
    Returning None for it would erase the sidecar from `topics()` and from every
    read. In a genuine multi-device recording every topic IS prefixed, so another
    device's topic still hits the `GILABS-` branch and is dropped as before.
    """
    if not device:
        return topic
    prefix = f"/{device}"
    if topic == prefix:
        return "/"
    if topic.startswith(prefix + "/"):
        return topic[len(prefix):]
    head = topic.split("/", 2)
    if len(head) >= 2 and head[1].startswith("GILABS-"):
        return None                 # genuinely another device's topic
    return topic                    # already canonical (a sidecar)


@cache
def _dynamic_message_class(name: str, data: bytes) -> type:
    """Build a message class from a schema's OWN embedded `FileDescriptorSet`.

    What makes a sidecar self-describing actually pay off: a container that does
    not ship the producing stage's generated module can still read its output.

    The pool is PRIVATE and never `descriptor_pool.Default()`. Adding a file whose
    name collides with an already-registered one raises out of the C++ pool and
    then poisons every later decode in the process — a failure that surfaces
    somewhere unrelated rather than at the file that caused it.
    """
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    fds = descriptor_pb2.FileDescriptorSet.FromString(data)
    pool = descriptor_pool.DescriptorPool()
    return message_factory.GetMessages(list(fds.file), pool=pool)[name]


def _resolve_message_class(schema) -> type:
    """The generated class for `schema.name`, else one from its embedded bytes."""
    try:
        return message_class(schema.name)
    except (KeyError, AttributeError):
        pass
    encoding = getattr(schema, "encoding", "")
    if encoding != "protobuf":
        raise ValueError(
            f"cannot decode schema {schema.name!r}: encoding {encoding!r} is not "
            f"protobuf. Only protobuf sidecars are readable.")
    return _dynamic_message_class(schema.name, bytes(schema.data))


def _detect_device(topics: Iterable[str]) -> str | None:
    """If every topic shares one ``/GILABS-XXXX/`` prefix, return that device."""
    devs = set()
    for t in topics:
        parts = t.split("/", 2)  # ["", "GILABS-XXXX", "rest..."]
        if len(parts) >= 2 and parts[1].startswith("GILABS-"):
            devs.add(parts[1])
        else:
            return None  # a non-prefixed topic -> not a device-prefixed file
    return next(iter(devs)) if len(devs) == 1 else None


class _FileIndex:
    """Cheap per-file index from the MCAP summary (no message scan)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.chan: dict[int, tuple[str, str]] = {}  # id -> (topic, schema_name)
        self.counts: dict[int, int] = {}
        self.start_ns: Ns | None = None
        self.end_ns: Ns | None = None
        # name -> the SCHEMA RECORD, not just its name: reading a derived topic
        # needs `.data`/`.encoding` to decode a message whose generated module this
        # process does not have, which is the whole point of an MCAP carrying its
        # schema inline.
        self.schema_rec: dict[str, object] = {}
        with open(path, "rb") as f:
            summary = make_reader(f).get_summary()
        if summary is None:
            self._scan()
            return
        schemas = {sid: s.name for sid, s in summary.schemas.items()}
        self.schema_rec = {s.name: s for s in summary.schemas.values()}
        for cid, c in summary.channels.items():
            self.chan[cid] = (c.topic, schemas.get(c.schema_id, ""))
        if summary.statistics is not None:
            self.counts = dict(summary.statistics.channel_message_counts)
            self.start_ns = summary.statistics.message_start_time or None
            self.end_ns = summary.statistics.message_end_time or None

    def _scan(self) -> None:
        """Fallback for a file with no summary section (rare): one pass."""
        with open(self.path, "rb") as f:
            for schema, channel, msg in make_reader(f).iter_messages():
                if schema is not None:
                    self.schema_rec.setdefault(schema.name, schema)
                self.chan[channel.id] = (channel.topic, schema.name if schema else "")
                self.counts[channel.id] = self.counts.get(channel.id, 0) + 1
                self.start_ns = msg.log_time if self.start_ns is None else self.start_ns
                self.end_ns = msg.log_time

    def topics(self) -> list[tuple[str, str, int]]:
        return [(t, s, self.counts.get(cid, 0)) for cid, (t, s) in self.chan.items()]


class Session:
    """Streaming, bounded-memory reader over a union of MCAP sources."""

    def __init__(
        self,
        *streams: Sequence[Path | str] | Path | str,
        device: str | None = None,
        reorder_ns: Ns = 50_000_000,
        adapters: Mapping[str, AdapterFactory] | None = None,
    ) -> None:
        """Each positional argument is one **stream**; they share a timeline.

        Within a stream the MCAPs are SEQUENTIAL and simply concatenated — a
        recording's chunks, ordered by first-message timestamp (not filename, §8.2).
        Between streams they are MERGED by timestamp, because they cover the same
        span on the same clock. That is exactly what a sidecar is, and it is why
        the distinction has to be declared rather than inferred:

            Session([ego_0000, ego_0001])                 # one stream, 2 chunks
            Session([ego_0000, ego_0001], [hands.mcap])   # + a derived stream

        Flattening the two into one list is what made a sidecar unreadable in
        practice: concatenated, every derived message lands after every frame, and
        the reorder buffer is tens of milliseconds — nowhere near a whole file — so
        the two never re-interleave. Measured before this was explicit, `sync`
        recovered 10 of 20 groups and silently dropped the rest.

        Resource cost is per STREAM, not per file: one open file and one decoder
        per stream at a time, so an 8-chunk session plus a sidecar holds two, not
        nine.

        `reorder_ns` is a **floor, not the final window** — see `_seam_slack`.
        A chunk boundary can hand back time, and the window has to cover that or
        the stream is silently out of order for every reader, not just one.
        """
        groups = [
            _expand_sources([s] if isinstance(s, str | Path) else s)
            for s in streams
        ]
        groups = [g for g in groups if g]
        if not groups:
            raise ValueError("Session: no .mcap files found in sources")
        flat: list[Path] = []
        owner: list[int] = []
        for sid, g in enumerate(groups):
            flat.extend(g)
            owner.extend([sid] * len(g))
        index = [_FileIndex(p) for p in flat]
        # Globally ordered by first-message timestamp: `topics`, calibration and
        # metadata read the flat list and want the recording's own order.
        order = sorted(range(len(flat)), key=lambda i: index[i].start_ns or 0)
        self._files = [flat[i] for i in order]
        self._index = [index[i] for i in order]
        owner = [owner[i] for i in order]
        # Which files belong to which stream, in the sorted order — the read path
        # concatenates within one of these and merges across them.
        self._streams = [
            [i for i, o in enumerate(owner) if o == sid] for sid in range(len(groups))
        ]
        self._reorder_ns = reorder_ns + self._seam_slack()
        self._device = device if device is not None else self._auto_device()
        self._calibration: Calibration | None = None
        self._metadata: SessionMeta | None = None
        self._exposure: dict[str, _ExposureTrack] | None = None
        self._exposure_cams_set: frozenset[str] | None = None
        self._cadence: dict[str, KeyframeCadence | None] = {}
        # Opt-in typed adapters for THIS session only. Deliberately not the global
        # registry: registering, say, a `foxglove.PoseInFrame` -> Pose adapter
        # globally would silently retype every existing consumer's elements, and a
        # consumer that asserts `isinstance(el, Record)` would start failing. So a
        # reader that wants richer types asks for them per session, and the
        # defaults stay exactly what they were.
        self._adapters = dict(adapters or {})
        # A typo in an override key is otherwise indistinguishable from "that
        # schema is not in this recording": you get Records, no error, and a pass
        # that silently yields nothing typed. Every key must name a schema some
        # file actually carries.
        if self._adapters:
            present = {s for idx in self._index for s in idx.schema_rec}
            unknown = sorted(set(self._adapters) - present)
            if unknown:
                raise ValueError(
                    f"adapters={unknown} name schemas no input carries "
                    f"(present: {sorted(present)}). Check the spelling — an "
                    f"override for an absent schema does nothing, silently."
                )

    def _seam_slack(self) -> Ns:
        """How far a chunk boundary hands time BACK, worst case, across streams.

        A recorder rolls to a new chunk without draining what it already holds,
        so chunk N+1's first message can predate chunk N's last by a wide margin
        — measured on a real 6-chunk ego session: **985, 374, 750, 74 and 316 ms**
        at the five seams. Within a chunk everything is ordered; only the seam
        inverts, and only between the two files that meet there.

        `_reorder`'s watermark follows arrival and is monotone, so once it has
        passed chunk N's tail those elements are gone — and chunk N+1 then yields
        earlier stamps into an already-advanced stream. At the old fixed 50 ms
        window that is a **silent** ordering break for every multi-chunk reader,
        not just one: `sync` drops pairs whose partner already left, a filter fed
        a backwards stamp rejects it, and a sidecar comes out non-monotone.

        The seam is a measurable property of the input, not a guess, and the
        per-file summaries are already indexed — so size the window from the data
        instead of picking a number big enough to hurt. Single-chunk sessions
        measure 0 and keep the caller's floor exactly; a session pays only for the
        overlap it actually has.

        Cost is real and worth stating: the heap holds roughly one window of
        elements, and in DECODED mode a stereo 1080p frame is ~6.2 MB, so a ~1 s
        window is a few hundred MB. `raw=True` holds the compressed AU (~35 KB)
        instead, which is why stages/merge can afford the same window cheaply.

        **A seam is a small overlap between otherwise sequential files**, and that
        is what makes this safe to do automatically. Two files that overlap for
        most of their span are not chunks at all — they are parallel streams that
        the caller flattened into one list, the mistake `__init__` documents. Left
        unchecked, that case measures an "overlap" of the entire recording and
        would buffer the whole file. So it is diagnosed and refused rather than
        accommodated: widening to a whole recording trades silent misordering for
        silent memory exhaustion, which is not a trade.
        """
        worst = 0
        for members in self._streams:
            for a, b in zip(members, members[1:], strict=False):
                ia, ib = self._index[a], self._index[b]
                if None in (ia.end_ns, ia.start_ns, ib.end_ns, ib.start_ns):
                    continue  # no summary; _scan() left the bounds unknown
                overlap = ia.end_ns - ib.start_ns
                if overlap <= 0:
                    continue
                # Compare against the FIRST file's span, not the shorter of the
                # two: a recording stopped shortly after a roll leaves a tiny
                # final chunk, and against that span an ordinary 1 s seam looks
                # like a 50% overlap — which would refuse to widen and hand back
                # the exact silent misordering this exists to fix. Parallel
                # streams also START together; chunks start a chunk apart.
                span = ia.end_ns - ia.start_ns
                started_together = abs(ib.start_ns - ia.start_ns) < overlap
                if started_together and overlap > span * _PARALLEL_OVERLAP_FRAC:
                    _log.warning(
                        "%s and %s overlap over %.0f%% of their span — these look "
                        "like PARALLEL streams flattened into one list, not chunks "
                        "of one. Pass them as separate arguments "
                        "(Session.open(rec, sidecars=[...])) or they will not "
                        "re-interleave.",
                        ia.path.name, ib.path.name, 100.0 * overlap / max(span, 1),
                    )
                    continue
                worst = max(worst, overlap)
        if worst > _MAX_SEAM_SLACK_NS:
            _log.warning(
                "chunk seams overlap by up to %.1f s, past the %.0f s ceiling — "
                "clamping. Elements beyond the clamp may still be emitted out of "
                "order; these files may not be chunks of one stream.",
                worst / 1e9, _MAX_SEAM_SLACK_NS / 1e9)
            worst = _MAX_SEAM_SLACK_NS
        elif worst > 0:
            _log.info(
                "chunk seams overlap by up to %.0f ms; widening the reorder "
                "window to cover it", worst / 1e6,
            )
        return worst

    @classmethod
    def open(
        cls,
        recording: Path | str,
        *,
        sidecars: Sequence[Path | str] = (),
        device: str | None = None,
        reorder_ns: Ns = 50_000_000,
        adapters: Mapping[str, AdapterFactory] | None = None,
    ) -> Session:
        """A read-only recording plus sidecars from any (writable) root.

        ``adapters`` maps a wire schema name to an adapter factory, adding typed
        elements for THIS session only — see `visio_schema.reader.adapters`.
        """
        # Each sidecar is its OWN stream: it spans the recording rather than
        # following it, so it must merge against the chunks, not append to them.
        return cls(recording, *sidecars, device=device, reorder_ns=reorder_ns,
                   adapters=adapters)

    # --- device ---------------------------------------------------------- #
    def _auto_device(self) -> str | None:
        """The device the RECORDING is prefixed with, ignoring canonical files.

        Detected PER FILE, not over the union. `SidecarWriter` writes canonical
        topics, and `_detect_device` returns None on the first non-prefixed topic
        it sees — so a union that included any sidecar used to collapse to None
        here, after which every recording topic failed to resolve and `stream()`
        went quiet. A file that carries no `/GILABS-*` prefix now contributes
        nothing rather than vetoing.

        Two files that disagree still give None: picking one arbitrarily is the
        kind of guess `_pick_stereo` refuses to make.
        """
        devs = {
            d
            for idx in self._index
            if (d := _detect_device(t for (t, _s, _n) in idx.topics())) is not None
        }
        return next(iter(devs)) if len(devs) == 1 else None

    @property
    def device(self) -> str | None:
        return self._device

    # --- cheap indexed metadata (summary read; NO decode pass) ----------- #
    def topics(self) -> list[TopicInfo]:
        agg: dict[str, tuple[str, int]] = {}
        for idx in self._index:
            for topic, schema, n in idx.topics():
                canon = strip_device_topic_prefix(topic, self._device)
                if canon is None:
                    continue
                schema0, n0 = agg.get(canon, (schema, 0))
                agg[canon] = (schema or schema0, n0 + n)
        return [TopicInfo(t, s, n) for t, (s, n) in sorted(agg.items())]

    def schema_conflicts(self) -> dict[str, list[str]]:
        """Topics carrying more than one schema across the union, if any.

        `topics()` cannot show this: it aggregates per canonical topic and keeps
        the first schema it saw, so two inputs disagreeing look like one input.
        That is fine for counting and wrong for merging — half the messages would
        decode against the wrong descriptor and a viewer renders that without
        complaining. `SidecarWriter.channel` refuses it, but only on the message
        that reaches it, which for a full copy is minutes in.

        Empty on every recording this repo produces; reachable the moment a union
        includes a sidecar from somewhere else.
        """
        seen: dict[str, list[str]] = {}
        for idx in self._index:
            for topic, schema, _n in idx.topics():
                canon = strip_device_topic_prefix(topic, self._device)
                if canon is None or not schema:
                    continue
                if schema not in seen.setdefault(canon, []):
                    seen[canon].append(schema)
        return {t: v for t, v in seen.items() if len(v) > 1}

    @property
    def calibration(self) -> Calibration:
        if self._calibration is None:
            self._calibration = self._read_calibration()
        return self._calibration

    @property
    def metadata(self) -> SessionMeta:
        if self._metadata is None:
            self._metadata = self._read_metadata()
        return self._metadata

    def metadata_records(self) -> list[tuple[str, dict[str, str]]]:
        """Every MCAP metadata record across the union, verbatim, in file order.

        `metadata` above is the *interpreted* view (device, span, capture dict);
        this is the raw set, for a consumer that has to carry provenance forward
        rather than read it — stages/merge, so a merged file keeps each
        sidecar's `visio.derived` record instead of dropping it.

        Names may repeat and that is legal: `Writer.add_metadata` has no
        uniqueness check and appends one index entry per call, so N sidecars
        contribute N `visio.derived` records and all N read back. Returned as a
        list of pairs, never a dict, for exactly that reason.
        """
        out: list[tuple[str, dict[str, str]]] = []
        for p in self._files:
            with open(p, "rb") as f:
                for m in make_reader(f).iter_metadata():
                    out.append((m.name, dict(m.metadata)))
        return out

    def metadata_record(self, name: str, **must_match: str) -> dict[str, str] | None:
        """The one metadata record with this `name` and these fields, else None.

        Raises when several match: names legitimately repeat (`metadata_records`
        explains why), so "the first one" is a coin toss between two sidecars that
        may describe different things. A consumer keying provenance off this cannot
        tell a wrong answer from a right one, so it must not get a wrong one.
        """
        hits = [kv for got, kv in self.metadata_records()
                if got == name
                and all(kv.get(k) == v for k, v in must_match.items())]
        if len(hits) > 1:
            where = " and ".join(f"{k}={v!r}" for k, v in must_match.items())
            raise ValueError(
                f"{len(hits)} {name!r} metadata records"
                + (f" matching {where}" if must_match else "")
                + " in this session — cannot tell which describes the topic being "
                  "read. Pass one sidecar at a time."
            )
        return hits[0] if hits else None

    # --- per-frame exposure (frame_info), attached to every Frame ------- #
    def _exposure_tracks(self) -> dict[str, _ExposureTrack]:
        """Camera topic -> its exposure timeline; empty when the stream is off.

        Materialized rather than streamed because interpolating a gap needs the
        entry AFTER it, and attaching at `Frame` construction means the entry must
        already be in hand. A streaming join is possible — measured on a real
        capture, frame_info lands within ±1 message of its video frame, well inside
        the reorder window — but it would have to attach at *release* time instead,
        which is a restructure this does not need.

        ⚠ NOT cheap on a recording that enables the stream. frame_info is one
        message per frame, so it appears in EVERY mcap chunk: `iter_messages` reads
        and un-chunks all of them, measured at 484 MB of reads (99.8% of the file)
        to collect 0.6 MB of payloads, ~0.4 s warm. That is why the caller gates
        this on actually streaming a camera. It is a real second pass over the
        bytes — "never a decode pass" is true only of HEVC.

        Memory is metadata-scale but not free: measured 415 B retained per entry
        (565 B peak), so ~45 MB for a 30-minute stereo recording. The "never load
        the episode" rule (``docs/alignment.md`` §0.2) is about decoded frames,
        which stay streaming.
        """
        if self._exposure is not None:
            return self._exposure
        want = [t for idx in self._index for (t, s, _n) in idx.topics()
                if s == FRAME_INFO_SCHEMA and t.endswith(FRAME_INFO_SUFFIX)]
        if not want:
            self._exposure = {}
            return self._exposure
        by_cam: dict[str, tuple[list[Ns], list[FrameExposure]]] = {}
        # Its OWN read, deliberately not the streaming one: an entry must be in
        # hand before the `Frame` it attaches to is constructed, so it cannot come
        # from the stream it is feeding, and interpolating a gap needs the entry
        # after it. One shared instance is safe here because `_parse_frame_info`
        # copies out immediately — unlike a `Record`, nothing escapes.
        proto = message_class(FRAME_INFO_SCHEMA)()
        want = sorted(set(want))
        for path, idx in zip(self._files, self._index, strict=True):
            if not any(s == FRAME_INFO_SCHEMA for (_t, s, _n) in idx.topics()):
                continue  # a sidecar, or a chunk recorded before the stream was on
            with open(path, "rb") as f:
                for schema, ch, msg in make_reader(f).iter_messages(topics=want):
                    if schema is None or schema.name != FRAME_INFO_SCHEMA:
                        continue
                    canon = strip_device_topic_prefix(ch.topic, self._device)
                    if canon is None:
                        continue
                    proto.ParseFromString(msg.data)
                    ts, exps = by_cam.setdefault(
                        _strip_suffix(canon, FRAME_INFO_SUFFIX), ([], []))
                    ts.append(msg.log_time)
                    exps.append(_parse_frame_info(proto))
        out: dict[str, _ExposureTrack] = {}
        for cam, (ts, exps) in by_cam.items():
            # Chunks are read in first-message order and entries are monotonic
            # within one, so this is normally already sorted — check before
            # permuting rather than rebuilding both lists unconditionally.
            if any(a > b for a, b in zip(ts, ts[1:], strict=False)):
                order = sorted(range(len(ts)), key=ts.__getitem__)
                ts = [ts[i] for i in order]
                exps = [exps[i] for i in order]
            _reject_duplicate_stamps(cam, ts)
            out[cam] = _ExposureTrack(ts, exps)
        self._exposure = out
        return out

    def _calib_topics(self) -> list[str]:
        """Calibration topics only — schema alone is not enough.

        A recording also carries `/tf` (`foxglove.FrameTransform`), which is a
        *runtime pose stream* (`world -> ego/imu/0`), not calibration: thousands of
        messages per file. Matching on schema alone seeks and parses every one of
        them on each `.calibration` access, and lands them in the same dict the
        extrinsics are picked from. Require the calibration topic suffix too.
        """
        out = []
        for idx in self._index:
            for topic, schema, _n in idx.topics():
                if schema in _CALIB_SCHEMAS and topic.endswith(_CALIB_SUFFIXES):
                    out.append(topic)
        return sorted(set(out))

    def _read_calibration(self) -> Calibration:
        cams: dict[str, CameraCalib] = {}
        stereo_R = stereo_T = None
        baseline = cam_imu_dt = imu_rate = None
        accel_nd = gyro_nd = None
        want = self._calib_topics()
        if not want:
            return Calibration(cams=cams)
        seen_tf: dict[str, tuple] = {}
        for schema_name, topic, msg in self._iter_calib(want):
            canon = strip_device_topic_prefix(topic, self._device)
            if canon is None:
                continue
            if schema_name == CAM_CALIB_SCHEMA and _cam_key(canon) not in cams:
                cams[_cam_key(canon)] = _parse_camera_calib(canon, msg)
            elif schema_name == FRAME_TF_SCHEMA and canon not in seen_tf:
                seen_tf[canon] = _parse_frame_transform(msg)
            elif schema_name == IMU_CALIB_SCHEMA and cam_imu_dt is None:
                cam_imu_dt, imu_rate, accel_nd, gyro_nd = _parse_imu_calib(msg)
        stereo = _pick_stereo(seen_tf)
        if stereo is not None:
            stereo_R, stereo_T = stereo
            baseline = float(np.linalg.norm(stereo_T))
        return Calibration(
            cams=cams,
            stereo_R=stereo_R,
            stereo_T=stereo_T,
            baseline_m=baseline,
            T_cam_imu=_pick_cam_imu(seen_tf),
            cam_imu_dt_ns=cam_imu_dt,
            imu_rate_hz=imu_rate,
            accel_noise_density=accel_nd,
            gyro_noise_density=gyro_nd,
        )

    def _iter_calib(self, want_topics: list[str]) -> Iterator[tuple[str, str, object]]:
        """Seek only the calib topics (chunk-index seek), first file wins."""
        for path in self._files:
            got = False
            with open(path, "rb") as f:
                reader = make_reader(f)
                for schema, channel, msg in reader.iter_messages(topics=want_topics):
                    if schema is None:
                        continue
                    proto = message_class(schema.name)()
                    proto.ParseFromString(msg.data)
                    yield schema.name, channel.topic, proto
                    got = True
            if got:
                return

    def _read_metadata(self) -> SessionMeta:
        capture: dict[str, str] = {}
        for path in self._files:
            with open(path, "rb") as f:
                for md in make_reader(f).iter_metadata():
                    if md.name == CAPTURE_METADATA:
                        capture = dict(md.metadata)
                        break
            if capture:
                break
        starts = [i.start_ns for i in self._index if i.start_ns is not None]
        ends = [i.end_ns for i in self._index if i.end_ns is not None]
        return SessionMeta(
            device=self._device,
            start_ns=min(starts) if starts else None,
            end_ns=max(ends) if ends else None,
            capture=capture,
        )

    # --- the single forward streaming pass ------------------------------- #
    def stream(
        self,
        topics: Sequence[str] | None = None,
        *,
        start_ns: Ns | None = None,
        end_ns: Ns | None = None,
        gray: bool = False,
        gpu: bool = False,
        raw: bool = False,
    ) -> Iterator[Element]:
        """Yield ``Frame | ImuSample | Record`` in strict capture-time order.

        ``topics`` selects canonical (prefix-stripped) topics; ``None`` = all
        decodable (video + IMU). ``gray=True`` decodes mono8 (VIO). Bounded
        memory: only a handful of decoded frames + one reorder window live.

        ``gpu=True`` selects the NVDEC device-resident RGB decoder for the
        **camera** streams (the depth fast-path): frames come out as on-GPU cupy
        arrays with the *same* timestamps and frame set as the CPU path. This is
        a construction-time choice — the decoder class is bound once here, ops
        downstream never branch on it. Camera-only: NVDEC's deep pipeline delays
        frames relative to IMU, so ``gpu`` is not for mixed camera+IMU streaming
        (VIO stays on the CPU/PyAV path); ``gray`` is unsupported with ``gpu``.

        ``raw=True`` is **passthrough**: every topic, no decoding at all. Each
        message comes back as a `Record` whose `data` holds the wire payload and
        whose `msg` is None, so it can be written straight into another MCAP with
        no parse and no re-serialize. With ``topics=None`` it selects EVERYTHING
        rather than the decodable set — the only mode that does, because the point
        is to reproduce a file rather than to interpret it. It also keeps the
        reorder heap cheap: a compressed H.265 AU is ~35 KB against a decoded
        1080p frame's ~6.2 MB, which is what makes a seam-sized window affordable.
        """
        if raw and (gray or gpu):
            raise ValueError(
                "stream(raw=True) does not decode, so `gray`/`gpu` — which only "
                "choose a decoder — cannot apply"
            )
        want = set(topics) if topics is not None else None
        if gpu and want is not None:
            # NVDEC has no MJPEG entry, so a named image topic would otherwise raise
            # from inside the generator on its first message, nowhere near the call
            # that chose `gpu`. Scoped to NAMED topics: an unnamed stills sidecar is
            # not in `_DECODABLE`, so a bare `stream(gpu=True)` is unaffected and the
            # depth path keeps working on any session that unions one.
            stills = sorted(set(self._topics_of_schema(IMAGE_SCHEMA)) & want)
            if stills:
                raise ValueError(
                    f"stream(gpu=True) cannot decode {IMAGE_SCHEMA} topics — NVDEC "
                    f"has no MJPEG decoder. Drop gpu=True for: {', '.join(stills)}"
                )
        # `raw` decodes nothing, so binding a backend would raise on a combination
        # the check above has already excused.
        make_decoders = None if raw else self._decoder_binder(gray, gpu, "stream")
        # Eager checks, lazy body — the shape `keyframe_stream` uses, and why every
        # check above fires at the CALL rather than on the first `next()`.
        return self._stream_elements(
            want, start_ns=start_ns, end_ns=end_ns,
            make_decoders=make_decoders, raw=raw)

    def _stream_elements(
        self, want: set[str] | None, *, start_ns: Ns | None, end_ns: Ns | None,
        make_decoders, raw: bool,
    ) -> Iterator[Element]:
        def _one_stream(members: list[int]) -> Iterator[tuple[Ns, Ns, Element]]:
            # Sequential: a stream's chunks do not overlap, and decoders reset per
            # chunk (each carries its own keyframes).
            for i in members:
                yield from self._iter_file(
                    self._files[i], self._index[i], want, make_decoders, raw)

        def _raw() -> Iterator[tuple[Ns, Ns, Element]]:
            # Merged ACROSS streams by arrival, concatenated within one. Only one
            # file per stream is open at a time, so the cost is per stream rather
            # than per file. Keyed on arrival (item[1]) to match `_reorder`'s
            # watermark, which follows arrival and not the element's own `t_ns`.
            gens = [_one_stream(m) for m in self._streams if m]
            if len(gens) == 1:
                yield from gens[0]
                return
            yield from heapq.merge(*gens, key=lambda it: it[1])

        # start/end and `t_ns` are the same clock — both are log_time (the bounds
        # come from SessionMeta's summary message_start_time/message_end_time).
        for el in _reorder(_raw(), self._reorder_ns):
            if _in_window(el.t_ns, start_ns, end_ns):
                yield el

    def _wanted_prefixed(
        self, idx: _FileIndex, want: set[str] | None, raw: bool = False
    ) -> list[str]:
        """Prefixed topic names to read from this file.

        ``want is None`` -> the decodable set only (video + IMU), unchanged: a bare
        ``stream()`` over a session that unions sidecars must not suddenly start
        emitting every derived topic.

        ``want`` given -> exactly what was asked for, **any schema**. Naming a
        derived topic IS the opt-in, so a sidecar reads back identically to a
        native stream and `sync` can match it to its frames. It used to be
        silently dropped here, which is the defect `docs/output.md` recorded.
        """
        out = []
        for topic, schema, _n in idx.topics():
            canon = strip_device_topic_prefix(topic, self._device)
            if canon is None:
                continue
            if raw and want is None:
                out.append(topic)  # passthrough: EVERY topic, decodable or not
            elif want is None:
                if schema in _DECODABLE:
                    out.append(topic)
            elif canon in want:
                out.append(topic)
        return out

    def _iter_file_raw(
        self, path: Path, wanted: list[str]
    ) -> Iterator[tuple[Ns, Ns, Element]]:
        """Passthrough: no decoder, no descriptor resolution, no parse.

        Split out from `_iter_file` rather than branched inside it, because the
        point is everything this does NOT do — the decoder construction below
        would open PyAV per chunk for streams nobody is going to decode, and the
        descriptor resolution would demand a generated module for schemas we are
        only copying. `t_ns == arrival` here: nothing expands into the future the
        way an IMU bundle does, so the pair is degenerate on purpose.
        """
        with open(path, "rb") as f:
            for schema, ch, msg in make_reader(f).iter_messages(topics=wanted):
                if schema is None:
                    continue
                canon = strip_device_topic_prefix(ch.topic, self._device)
                t = msg.log_time
                yield t, t, Record(canon, t, schema.name, None, msg.data)

    def _build_adapters(self, idx: _FileIndex, wanted: set[str], make_decoders) -> dict:
        """One adapter per schema this pass will meet, built before the first read.

        Video and stills are bound unconditionally and IN THAT ORDER: `flush`
        iterates this dict, and the NVDEC tail has to drain video before stills,
        which is the order the dispatch chain this replaced flushed in. Binding a
        decoder for a schema the file does not carry is free — its decoder dict
        stays empty and its `flush` yields nothing.

        Derived schemas resolve here too, so an unresolvable one fails on the
        first read OF ITS FILE rather than partway through it. Not at open: this
        runs per chunk, so chunk 6 of 8 still fails on chunk 6.
        """

        def class_for(name: str) -> type:
            # Either the global descriptor pool (types this reader knows by name)
            # or the file's own embedded FileDescriptorSet (anything derived), so
            # a sidecar carrying a type this process never imported still reads.
            if name in _DECODED:
                return message_class(name)
            return _resolve_message_class(idx.schema_rec[name])

        ctx = AdapterContext(make_decoders=make_decoders, message_class_for=class_for)
        over = self._adapters
        adapters = {
            name: build_adapter(name, ctx, overrides=over)
            for name in (VIDEO_SCHEMA, IMAGE_SCHEMA, IMU_RAW_SCHEMA)
        }
        for topic, schema_name, _n in idx.topics():
            if topic not in wanted or schema_name in adapters:
                continue
            if schema_name not in _DECODED and schema_name in idx.schema_rec:
                adapters[schema_name] = build_adapter(schema_name, ctx, overrides=over)
        return adapters

    def _iter_file(
        self, path: Path, idx: _FileIndex, want: set[str] | None,
        make_decoders, raw: bool = False,
    ) -> Iterator[tuple[Ns, Ns, Element]]:
        # Topic-filtered read: skip the discarded majority (IMU-quat, audio, …)
        # instead of building a Python object per message like read_mcap. The
        # MCAP `log_time` IS the NTP-corrected wire stamp (see the module
        # docstring), so use it directly and unmodified.
        # Each element carries (t_ns, arrival_ns, element). They differ only for
        # IMU: a bundle expands to samples up to ~1 s past the bundle's own
        # arrival, and the reorder watermark must follow arrival rather than
        # those expanded sample times — see _reorder.
        wanted = self._wanted_prefixed(idx, want, raw)
        if not wanted:
            return
        if raw:
            yield from self._iter_file_raw(path, wanted)
            return
        adapters = self._build_adapters(idx, set(wanted), make_decoders)
        with open(path, "rb") as f:
            for schema, ch, msg in make_reader(f).iter_messages(topics=wanted):
                if schema is None:
                    continue
                adapter = adapters.get(schema.name)
                if adapter is None:
                    continue
                canon = strip_device_topic_prefix(ch.topic, self._device)
                yield from adapter.emit(msg.data, canon, msg.log_time)
        for adapter in adapters.values():
            yield from adapter.flush()

    # --- sparse video: keyframes only, so P-frames are never decoded ----- #
    def keyframe_cadence(self, topic: str) -> KeyframeCadence | None:
        """Measure how often ``topic`` carries a keyframe. ``None`` if unmeasurable.

        The query half of the sparse-sampling pair: ask what the bitstream offers,
        then ask :meth:`keyframe_stream` for every n-th of them. Split in two
        deliberately — the caller owns the policy (round down? refuse? warn?), and
        a `stream()`-style knob that silently picked one would hide the fact that
        the achievable rate is quantised by the GOP.

        A bounded probe: it parses AU headers (never pixels) until it has
        `_CADENCE_KEYFRAMES` keyframes or has looked at `_CADENCE_AUS` of them,
        walking on to the next chunk if the first holds under two. Cached per
        topic, so the read that follows does not pay for it again.

        ``None`` means exactly one thing: **fewer than two keyframes**, so there is
        no spacing to measure — for a healthy recording, a clip shorter than one
        GOP, where `every_n = 1` is the right answer anyway. A codec with no parser
        here raises instead, from this method and from `keyframe_stream` alike, so
        the two entry points fail identically rather than one of them reporting
        "short clip" for an AV1 file the other will refuse outright.
        """
        if topic not in self._cadence:
            self._require_video_topic(topic)
            self._cadence[topic] = self._measure_cadence(topic)
        return self._cadence[topic]

    def _measure_cadence(self, topic: str) -> KeyframeCadence | None:
        video = message_class(VIDEO_SCHEMA)()
        for path, prefixed in self._video_files(topic):
            stamps: list[Ns] = []
            positions: list[int] = []
            for n, (t_ns, key) in enumerate(
                self._iter_video_aus(path, prefixed, topic, video)
            ):
                if key:
                    stamps.append(t_ns)
                    positions.append(n)
                if len(stamps) >= _CADENCE_KEYFRAMES or n + 1 >= _CADENCE_AUS:
                    break
            if len(stamps) < 2:
                continue  # a chunk shorter than one GOP; try the next
            period_ns = int(round(float(np.median(np.diff(stamps)))))
            if period_ns <= 0:
                continue  # duplicate stamps — `every_n_for` would divide by this
            return KeyframeCadence(
                topic=topic,
                period_ns=period_ns,
                frames_per_gop=int(round(float(np.median(np.diff(positions))))),
                sampled=len(stamps),
            )
        return None

    def _video_files(self, topic: str, *, start_ns=None, end_ns=None):
        """Each file that can carry `topic` in `[start_ns, end_ns)`, in order.

        Files whose indexed span misses the window are never opened — the summary
        already holds their bounds, so a short window over a long session costs
        the chunks it overlaps rather than all of them.
        """
        for path, idx in zip(self._files, self._index, strict=True):
            prefixed = self._video_topic_in(idx, topic)
            if prefixed is None or not _spans_window(idx, start_ns, end_ns):
                continue
            yield path, prefixed

    def _iter_video_aus(
        self, path: Path, prefixed: str, canon: str, video, *,
        start_ns: Ns | None = None, end_ns: Ns | None = None,
    ) -> Iterator[tuple[Ns, bool]]:
        """Yield ``(t_ns, is_keyframe)`` per access unit of one file's video topic.

        ⚠ ``video`` is a SHARED buffer holding the current AU, exactly as
        ``_iter_file`` uses it: a consumer must read ``video.data`` / ``.format`` /
        ``.frame_id`` before asking for the next item. That aliasing is what lets
        the probe and the decode path run the same loop without either of them
        copying a 33 KB payload per message.

        The window is pushed into ``iter_messages``, whose ``[start, end)`` is the
        same half-open bound ``_in_window`` applies, so out-of-window chunks are
        skipped through the message index instead of read and discarded.
        """
        with open(path, "rb") as f:
            messages = make_reader(f).iter_messages(
                topics=[prefixed], start_time=start_ns, end_time=end_ns)
            for _schema, _ch, msg in messages:
                video.ParseFromString(msg.data)
                key = is_keyframe(video.format, video.data)
                if key is None:
                    # A property of the codec, so in practice this fires on the
                    # first AU, before any frame has reached the consumer.
                    raise ValueError(
                        f"cannot locate keyframes in {video.format!r} on {canon} — "
                        f"only {', '.join(KEYFRAME_FORMATS)} are parsed. Use "
                        f"Session.stream(), which decodes every frame and so needs "
                        f"no keyframe knowledge."
                    )
                yield msg.log_time, key

    def keyframe_stream(
        self,
        topic: str,
        *,
        every_n: int = 1,
        start_ns: Ns | None = None,
        end_ns: Ns | None = None,
        gray: bool = False,
        gpu: bool = False,
    ) -> Iterator[Frame]:
        """Every n-th keyframe of ONE video topic, decoding nothing else.

        The sparse-sampling path — a VLM that embeds a frame per second wants ~3%
        of the pictures, and `stream()` cannot give it them cheaply because H.265
        here is all-P: a delta frame is only reachable by reconstructing every
        frame back to its keyframe. A *keyframe* is not, so this skips the other
        29 AUs before the decoder sees them. Measured 6.1x on a real ego chunk,
        with the emitted images byte-identical to the full-decode ones.

        Not a mode of `stream()`. `stream()` is the general merged-timeline source
        — many topics, IMU interleaved, reorder buffer, `Element` union — and none
        of that applies here: one topic, one decoder, frames only. Splitting it out
        is the same call `_iter_file_raw` makes, for the same reason: what this
        does not do is the point.

        `every_n` counts **keyframes**, which is a frame-count rule and so gives up
        the property `stream()` guards for alignment: a lost keyframe re-phases the
        remainder. That is real and is why the general path buckets on `t_ns`
        instead; it is accepted here because the one case this exists for is
        `every_n = 1` (a ~1 s GOP under a ~1 Hz target), where there is no phase to
        lose. Ask :meth:`keyframe_cadence` for the divisor rather than assuming a
        GOP length.

        `gray` / `gpu` select the same decoders `stream()` uses, and every frame
        carries the same `t_ns`, `frame_id` and `exposure` it would there. The
        window is applied to the AU before the count, so `start_ns` anchors the
        phase at the window rather than slicing a stream-wide one; the keyframe
        count itself runs across the session's chunks, so a chunk boundary does not
        restart it.

        No reorder buffer: one topic read forward through chunks ordered by first
        message. A chunk seam that hands time back (`_seam_slack`) can therefore
        show up as a non-monotone `t_ns` here rather than being smoothed — visible
        in the stamps, which is the honest failure for a consumer that cares.
        """
        if every_n < 1:
            raise ValueError(f"keyframe_stream: every_n must be >= 1, got {every_n}")
        self._require_video_topic(topic)
        # A plain function returning the generator, so these checks and the decoder
        # binding fire at the call site rather than on the first `next()` — a
        # mistyped topic is the likely error and should not surface an hour in.
        return self._keyframe_frames(
            topic, every_n=every_n, start_ns=start_ns, end_ns=end_ns,
            make_decoders=self._decoder_binder(gray, gpu, "keyframe_stream"),
        )

    def _keyframe_frames(
        self, topic: str, *, every_n: int, start_ns: Ns | None, end_ns: Ns | None,
        make_decoders,
    ) -> Iterator[Frame]:
        # ONE counter for the whole call: a per-file one would restart the phase at
        # every chunk boundary, a re-phase the caller never asked for.
        seen = aus = 0
        video = message_class(VIDEO_SCHEMA)()
        for path, prefixed in self._video_files(
            topic, start_ns=start_ns, end_ns=end_ns
        ):
            # A fresh decoder pair per chunk, as `_iter_file` binds one — free of
            # consequence here, since every AU fed is self-contained anyway.
            # `flush()` still matters: NVDEC holds frames in flight per chunk.
            emit, flush = make_decoders(video)
            for _t_ns, key in self._iter_video_aus(
                path, prefixed, topic, video, start_ns=start_ns, end_ns=end_ns
            ):
                aus += 1
                if not key:
                    continue
                take = seen % every_n == 0
                seen += 1
                if take:
                    yield from (el for _t, _arrival, el in emit(topic, _t_ns))
            yield from (el for _t, _arrival, el in flush())
        if aus and not seen:
            # Access units, but not one of them classified as a keyframe. Yielding
            # nothing would be indistinguishable from an empty recording, and this
            # is the one failure `is_keyframe`'s quiet `False` could otherwise hide
            # (a bitstream whose slices sit past the prefix bound, say).
            raise ValueError(
                f"keyframe_stream: {aus} access unit(s) on {topic} and no keyframe "
                f"among them — this stream cannot be sampled sparsely. Use "
                f"Session.stream() to decode it in full."
            )

    def _video_topic_in(self, idx: _FileIndex, canon: str) -> str | None:
        """This file's PREFIXED name for the canonical video topic, if it has it."""
        for topic, schema, _n in idx.topics():
            if schema != VIDEO_SCHEMA:
                continue
            if strip_device_topic_prefix(topic, self._device) == canon:
                return topic
        return None

    def _topics_of_schema(self, schema: str) -> list[str]:
        """Canonical topics carrying `schema`, sorted. Index-only, no file read."""
        return sorted(t.topic for t in self.topics() if t.schema_name == schema)

    def _require_video_topic(self, topic: str) -> None:
        """Fail before any read if `topic` is not a video stream in this session.

        Named topics are how every other read opts in, so a typo is the likely
        error, and its silent form — an empty iterator — is indistinguishable from
        a recording whose camera never produced anything.
        """
        present = self._topics_of_schema(VIDEO_SCHEMA)
        if topic in present:
            return
        raise ValueError(
            f"{topic!r} is not a {VIDEO_SCHEMA} topic in this session "
            f"(device={self._device}); video topics present: "
            f"{', '.join(present) or 'none'}"
        )

    def _exposure_cams(self) -> frozenset[str]:
        """Canonical topics that HAVE a `frame_info` sibling, straight off the index.

        The cheap half of the question `_exposure_at` asks. Answering "is there any
        exposure for this topic at all" from the summary means a topic that has
        none — every derived topic, and every camera on a recording with the stream
        off — never triggers `_exposure_tracks`' second pass over the bytes.
        """
        if self._exposure_cams_set is None:
            self._exposure_cams_set = frozenset(
                strip_device_topic_prefix(t, self._device)[: -len(FRAME_INFO_SUFFIX)]
                for idx in self._index for (t, s, _n) in idx.topics()
                if s == FRAME_INFO_SCHEMA and t.endswith(FRAME_INFO_SUFFIX)
            )
        return self._exposure_cams_set

    def _exposure_at(self, canon: str, t_ns: Ns) -> FrameExposure | None:
        """This camera's exposure for a frame, or None when the stream is off.

        Keyed on the FRAME's own stamp, never on the access unit being fed: the
        NVDEC decoder is deep-pipelined, so those are different frames.

        The index check comes first so a topic with no `frame_info` — a stills
        sidecar, say — costs nothing rather than triggering the materialising pass
        (`_exposure_tracks`: ~484 MB of reads) only to answer `None`.
        """
        if canon not in self._exposure_cams():
            return None
        track = self._exposure_tracks().get(canon)
        return track.at(t_ns) if track else None

    def _decoder_binder(self, gray: bool, gpu: bool, what: str):
        """Resolve the decode backend ONCE -> a ``video`` proto -> ``(emit, flush)``.

        Both read paths bind a per-chunk decoder pair from the same two flags, and
        `gray` is meaningless on the GPU side — so the refusal and the pixel-format
        choice live here rather than being restated, with drifting wording, wherever
        a pair is built. ``what`` only names the caller in that refusal.
        """
        if gpu and gray:
            raise ValueError(f"{what}(gpu=True) decodes RGB only; gray is CPU-path")
        if gpu:
            return self._gpu_video_decoders
        pixel_format = "gray" if gray else "rgb24"
        return lambda proto: self._cpu_video_decoders(proto, pixel_format)

    def _cpu_video_decoders(self, video, pixel_format: str):
        """Per-camera PyAV decoders: 1-in-1-out, stamped with the current AU's t."""
        decoders: dict[str, HevcDecoder] = {}

        def emit(canon: str, t: Ns) -> Iterator[tuple[Ns, Ns, Element]]:
            dec = decoders.get(canon)
            if dec is None:
                dec = decoders[canon] = HevcDecoder(video.format, pixel_format)
            img = dec.decode(video.data)
            if img is not None:
                yield t, t, Frame(canon, t, img, video.frame_id,
                                  exposure=self._exposure_at(canon, t))

        def flush() -> Iterator[tuple[Ns, Ns, Element]]:
            return
            yield  # pragma: no cover — CPU path holds no in-flight frames

        return emit, flush

    def _gpu_video_decoders(self, video):
        """Per-camera NVDEC decoders: device RGB frames self-timestamped by PTS."""
        from ._gpu_decode import NvDecoder

        decoders: dict[str, NvDecoder] = {}
        frame_ids: dict[str, str] = {}

        def emit(canon: str, t: Ns) -> Iterator[tuple[Ns, Ns, Element]]:
            dec = decoders.get(canon)
            if dec is None:
                dec = decoders[canon] = NvDecoder(video.format)
            frame_ids[canon] = video.frame_id
            # The decoder is deep-pipelined, so the frame coming out is an
            # EARLIER one than the AU going in: `pts` is its own stamp, `t` the
            # current AU's arrival.
            for pts, img, ev in dec.decode(video.data, t):
                yield pts, t, Frame(canon, pts, img, frame_ids[canon], event=ev,
                                    exposure=self._exposure_at(canon, pts))

        def flush() -> Iterator[tuple[Ns, Ns, Element]]:
            for canon, dec in decoders.items():
                for pts, img, ev in dec.flush():
                    # These are the last frames of the chunk; arrival = their own
                    # t is monotone-safe and they release in the reorder tail.
                    yield pts, pts, Frame(canon, pts, img, frame_ids.get(canon, ""),
                                          event=ev,
                                          exposure=self._exposure_at(canon, pts))

        return emit, flush


# --------------------------------------------------------------------------- #
# module helpers
# --------------------------------------------------------------------------- #
def _expand_sources(sources: Sequence[Path | str]) -> list[Path]:
    files: list[Path] = []
    for s in sources:
        p = Path(s)
        if p.is_dir():
            files.extend(sorted(p.glob("*.mcap")))
        elif p.suffix == ".mcap" and p.is_file():
            files.append(p)
    return files


def _in_window(t: Ns, start_ns: Ns | None, end_ns: Ns | None) -> bool:
    """Half-open ``[start_ns, end_ns)``; either bound may be absent."""
    return (start_ns is None or t >= start_ns) and (end_ns is None or t < end_ns)


def _spans_window(idx: _FileIndex, start_ns: Ns | None, end_ns: Ns | None) -> bool:
    """Could this file hold a message in ``[start_ns, end_ns)``?

    Conservative: a file whose summary left the bounds unknown (`_FileIndex._scan`
    on a file with no summary section) is always read.
    """
    if idx.start_ns is None or idx.end_ns is None:
        return True
    return ((end_ns is None or idx.start_ns < end_ns)
            and (start_ns is None or idx.end_ns >= start_ns))


def _cam_key(intrinsics_topic: str) -> str:
    """`/ego/camera/0/intrinsics` -> `/ego/camera/0` (the camera topic)."""
    return _strip_suffix(intrinsics_topic, "/intrinsics")


def _parse_camera_calib(intrinsics_topic: str, m) -> CameraCalib:
    return CameraCalib(
        topic=_cam_key(intrinsics_topic),
        width=int(m.width),
        height=int(m.height),
        model=m.distortion_model,
        K=np.array(m.K, float).reshape(3, 3),
        D=np.array(m.D, float),
        frame_id=m.frame_id,
        R=np.array(m.R, float).reshape(3, 3) if len(m.R) == 9 else None,
        P=np.array(m.P, float).reshape(3, 4) if len(m.P) == 12 else None,
    )


def _parse_frame_transform(m) -> tuple[str, np.ndarray, np.ndarray]:
    from scipy.spatial.transform import Rotation

    t = m.translation
    q = m.rotation
    R = Rotation.from_quat([q.x, q.y, q.z, q.w]).as_matrix()
    T = np.array([t.x, t.y, t.z], float)
    return m.child_frame_id, R, T


def _pick_stereo(tfs: dict[str, tuple]) -> tuple[np.ndarray, np.ndarray] | None:
    """The stereo extrinsic: the pose of cam1 in cam0, off cam1's extrinsics topic.

    Camera topics only, and no fall back to an arbitrary transform: on a recording
    lacking `camera/*/extrinsics` the first `/tf` is a runtime `world -> imu0` pose,
    and rectifying with that fails silently rather than loudly.
    """
    for topic, (_child, R, T) in tfs.items():
        if "/camera/" in topic and topic.endswith("/extrinsics"):
            return R, T
    return None


def _pick_cam_imu(tfs: dict[str, tuple]) -> np.ndarray | None:
    """``T_cam_imu``: the cam0 <- imu0 4x4, off the IMU's extrinsics topic.

    The device publishes kalibr's ``T_cam_imu`` **verbatim** as a FrameTransform
    with ``parent="cam0", child="imu0"`` (visio-setup `calib/push.py:154`), and
    foxglove's convention (child-frame point -> parent frame) agrees, so this is
    used directly with **no inversion**. Same rule as `_pick_stereo`: no fallback.
    """
    for topic, (_child, R, T) in tfs.items():
        if "/imu/" in topic and topic.endswith("/extrinsics"):
            return make_T(R, T)
    return None


def _parse_imu_calib(m) -> tuple[int | None, float | None, float | None, float | None]:
    off = m.time_offset_to_cam0_s
    dt_ns = int(round(off * 1e9)) if off else None
    rate = float(m.update_rate_hz) or None
    accel_nd = float(m.accel_noise_density) or None
    gyro_nd = float(m.gyro_noise_density) or None
    return dt_ns, rate, accel_nd, gyro_nd


class _ExposureTrack:
    """One camera's exposure timeline, with gap interpolation.

    Built from the recording's ``frame_info`` entries, which join their video frame
    by exact timestamp equality (visio-schema 0.7.0 guarantees that stamp is unique
    per stream, so the join is unambiguous). Entries can still be *missing* — the
    producer drops any it cannot bind to a captured frame, and the ISP genuinely
    loses stats entries — so ``at()`` never returns nothing for a frame inside the
    track: it interpolates and flags the result.

    Why interpolate rather than expose the gap: a consumer applying an
    exposure-derived timing correction would otherwise have that one frame snap back
    to uncorrected, a step of up to ``T_exp / 2``, which is precisely the jitter the
    correction exists to remove. AE moves slowly, so a bracketed estimate is wrong by
    a tiny fraction of that.
    """

    def __init__(self, ts: list[Ns], exposures: list[FrameExposure]) -> None:
        self._ts = ts
        self._exp = exposures

    def __len__(self) -> int:
        return len(self._ts)

    def at(self, t_ns: Ns) -> FrameExposure:
        i = bisect.bisect_left(self._ts, t_ns)
        if i < len(self._ts) and self._ts[i] == t_ns:
            return self._exp[i]  # exact — the common case
        # Outside the track on either end: hold the nearest. Linear extrapolation
        # of an AE curve past its last sample is a guess with no bound.
        if i == 0:
            return _as_interpolated(self._exp[0])
        if i == len(self._ts):
            return _as_interpolated(self._exp[-1])
        lo_t, hi_t = self._ts[i - 1], self._ts[i]
        lo, hi = self._exp[i - 1], self._exp[i]
        w = (t_ns - lo_t) / (hi_t - lo_t)
        return _lerp_exposure(lo, hi, w)


def _as_interpolated(e: FrameExposure) -> FrameExposure:
    return replace(e, isp_frame_id=None, interpolated=True)


def _lerp_exposure(lo: FrameExposure, hi: FrameExposure, w: float) -> FrameExposure:
    """Blend the AE outputs; carry sensor timing from the nearer neighbour.

    Only the continuously-varying AE quantities are interpolated. Sensor timing
    (line time, VTS) is static per sensor mode, so a fractional value would be
    meaningless rather than merely approximate; the same goes for the integer
    register/ISO fields, which are taken from the nearer side so they stay
    self-consistent with a real operating point.
    """
    near = hi if w >= 0.5 else lo

    def mix(a: float, b: float) -> float:
        return a + (b - a) * w

    # Built FROM `near` rather than field-by-field, so a field added later
    # inherits a real operating point instead of silently reverting to a default.
    return replace(
        near,
        exposure_time_s=mix(lo.exposure_time_s, hi.exposure_time_s),
        analog_gain=mix(lo.analog_gain, hi.analog_gain),
        digital_gain=mix(lo.digital_gain, hi.digital_gain),
        isp_digital_gain=mix(lo.isp_digital_gain, hi.isp_digital_gain),
        isp_frame_id=None,
        interpolated=True,
    )


def _reject_duplicate_stamps(cam: str, ts: list[Ns]) -> None:
    """A repeated frame_info timestamp is a wire-contract violation — say so.

    Schema 0.7.0 makes ``timestamp`` unique per stream *by construction* (the
    producer drops any entry it cannot bind rather than substituting a stamp), so
    a duplicate means two entries claim one frame and there is no way to know
    which is that frame's. Silently taking the first is exactly the failure the
    0.7.0 change removed — pre-0.7.0 recordings carried it on 4-7% of frames — so
    it fails loudly instead. Same policy as ``_pick_stereo``: refuse rather than
    pick arbitrarily.
    """
    dup = next((a for a, b in zip(ts, ts[1:], strict=False) if a == b), None)
    if dup is not None:
        raise ValueError(
            f"{cam}{FRAME_INFO_SUFFIX}: duplicate timestamp {dup} — two exposure "
            "entries claim one frame, so neither can be trusted. This recording "
            "predates visio-schema 0.7.0, whose producer drops an unbindable entry "
            "instead of stamping it with the drain frame's PTS."
        )


def _strip_suffix(topic: str, suffix: str) -> str:
    """A sibling topic (`.../frame_info`, `.../intrinsics`) -> its parent."""
    return topic[: -len(suffix)] if topic.endswith(suffix) else topic


def _parse_frame_info(m) -> FrameExposure:
    """One ``CameraFrameInfo`` -> :class:`FrameExposure`.

    ``line_time_ns = line_length_pixels / (pixel_clock_mhz * 1e6)``, per the proto.

    ``None`` when the sensor timing is absent, NOT 0.0: a zero row delay is the
    legitimate encoding of a *global-shutter* sensor, so returning it here would
    silently turn a rolling-shutter model into a no-op. This is reachable —
    ``AiqManager::prime_stats`` logs "HTS/pclk will read 0 on the wire" and gives
    up for the whole session when ``queryExpResInfo`` fails at prime time.
    """
    pclk_hz = float(m.pixel_clock_mhz) * 1e6
    line_ns = (float(m.line_length_pixels) / pclk_hz * 1e9) if pclk_hz > 0 else None
    return FrameExposure(
        exposure_time_s=float(m.exposure_time_s),
        line_time_ns=line_ns,
        frame_length_lines=int(m.frame_length_lines),
        analog_gain=float(m.analog_gain),
        digital_gain=float(m.digital_gain),
        isp_digital_gain=float(m.isp_digital_gain),
        iso=int(m.iso),
        coarse_integration_time_lines=int(m.coarse_integration_time_lines),
        isp_frame_id=int(m.isp_frame_id),
    )


def _reorder(
    items: Iterable[tuple[Ns, Ns, Element]], reorder_ns: Ns
) -> Iterator[Element]:
    """Release elements in ``t_ns`` order once safe (bounded watermark heap).

    Each item is ``(t_ns, arrival_ns, element)``. The watermark follows the
    **arrival** (message log_time), which is monotonic, NOT the element ``t_ns``:
    a single IMU bundle expands to samples whose ``t_ns`` reaches ~1 s past its
    arrival, but no *future* message can produce a ``t_ns`` below its own arrival,
    so an element is safe to release once the arrival watermark has passed its
    ``t_ns`` by ``reorder_ns`` (the slack covers chunk-seam arrival overlap).
    Bounded: at most ~one IMU-bundle span of samples (+ a couple of frames) buffered.

    The invariant is unconditional because ``t_ns`` is never shifted off the wire
    clock (see the module docstring).
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
