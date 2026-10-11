"""Join frame-info into one ordered sensor stream without a metadata scan."""

from __future__ import annotations

import bisect
from collections import deque
from dataclasses import replace

from .domain import Frame, Record
from .ops import _Matcher


def join_exposure(stream, cameras, *, emit_topics=(), max_gap_ns=1_000_000_000,
                  max_frame_bytes=256 * 1024 * 1024):
    # Session's parsers encode the same units/interpolation as the indexed path.
    from .session import _bind, _lerp_entry, _parse_frame_info

    info_topics = {cam + "/frame_info": cam for cam in cameras}
    matchers = {cam: _Matcher((cam, cam + "/frame_info"), tol_ns=0,
                              window=128, emit_partial=True) for cam in cameras}
    entries = {cam: deque() for cam in cameras}
    ready = {}
    pending = deque()
    image_bytes = 0

    def exposure(frame, final):
        key = (frame.topic, frame.t_ns)
        if key in ready:
            return ready.pop(key)
        track = entries[frame.topic]
        times = [t for t, _entry in track]
        i = bisect.bisect_left(times, frame.t_ns)
        if i < len(track):
            hi_t, hi = track[i]
            if hi_t == frame.t_ns:
                return _bind(hi, frame.t_ns, interpolated=False)
            if i == 0:
                if hi_t - frame.t_ns > max_gap_ns:
                    raise ValueError(f"{frame.topic}: frame_info leading gap exceeds limit")
                return _bind(hi, frame.t_ns, interpolated=hi_t != frame.t_ns)
            lo_t, lo = track[i - 1]
            if hi_t - lo_t > max_gap_ns:
                raise ValueError(f"{frame.topic}: frame_info interpolation gap exceeds limit")
            w = (frame.t_ns - lo_t) / (hi_t - lo_t)
            return _bind(_lerp_entry(lo, hi, w), frame.t_ns, interpolated=True)
        if final and track:
            if frame.t_ns - track[-1][0] > max_gap_ns:
                raise ValueError(f"{frame.topic}: frame_info tail gap exceeds limit")
            return _bind(track[-1][1], frame.t_ns, interpolated=True)
        return None

    def drain(watermark, final=False):
        nonlocal image_bytes
        while pending:
            el = pending[0]
            if isinstance(el, Frame) and el.topic in cameras:
                exp = exposure(el, final)
                if exp is not None and exp.interpolated and not final and watermark <= el.t_ns:
                    break  # another record at this exact timestamp may follow
                if exp is None:
                    if final or watermark - el.t_ns > max_gap_ns:
                        raise ValueError(f"{el.topic}: unresolved frame_info gap at {el.t_ns}")
                    break
                el = replace(el, exposure=exp)
            pending.popleft()
            if isinstance(el, Frame):
                image_bytes -= el.image.nbytes
            yield el
        frontier = pending[0].t_ns if pending else watermark
        for track in entries.values():
            while len(track) > 2 and track[1][0] < frontier:
                track.popleft()

    for el in stream:
        cam = info_topics.get(el.topic)
        if cam is not None and isinstance(el, Record):
            track = entries[cam]
            times = [t for t, _entry in track]
            at = bisect.bisect_left(times, el.t_ns)
            if at < len(track) and times[at] == el.t_ns:
                raise ValueError(f"{cam}: duplicate timestamp in frame_info")
            track.insert(at, (el.t_ns, _parse_frame_info(cam, el.t_ns, el.msg)))
        elif isinstance(el, Frame) and el.topic in cameras:
            cam = el.topic
        else:
            cam = None
        if cam is not None:
            # Matching needs only identity. Retaining pixels here as well as in
            # pending would hide another large camera buffer from the byte cap.
            identity = (Record(el.topic, el.t_ns, "", None)
                        if isinstance(el, Frame) else el)
            for matched in matchers[cam].push(identity):
                if not isinstance(matched, Frame | Record):
                    frame = matched.by_topic[cam].element
                    record = matched.by_topic[cam + "/frame_info"].element
                    entry = _parse_frame_info(cam, record.t_ns, record.msg)
                    ready[(cam, frame.t_ns)] = _bind(entry, frame.t_ns, interpolated=False)
        if el.topic not in info_topics or el.topic in emit_topics:
            pending.append(el)
            if isinstance(el, Frame):
                image_bytes += el.image.nbytes
        yield from drain(el.t_ns)
        if image_bytes > max_frame_bytes:
            raise ValueError("frame_info join exceeded pending image byte limit")
    yield from drain(pending[-1].t_ns if pending else 0, final=True)
