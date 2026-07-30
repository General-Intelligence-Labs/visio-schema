#!/usr/bin/env python3
"""Framerate stability and dropped-frame check for Visio MCAP recordings.

    pip install mcap mcap-protobuf-support numpy
    python mcap_rate_check.py recording.mcap [more.mcap ...|dir|glob]

Reads the schemas embedded in the MCAP, so no visio_schema install is needed, and
works on any recording regardless of which schema version produced it.

Covers the camera streams (foxglove.CompressedVideo), the IMU fusion output
(/imu/<i>/quat) and the bundled raw IMU (/imu/<i>/raw), plus audio and any other
timestamped stream. For each it reports the true rate, the jitter, and every gap
big enough to be a dropped sample -- then a verdict.

Four traps this gets right, all of which manufacture fake faults if missed:

  * /imu/<i>/raw is BUNDLED. One ImuRaw message carries N ImuRawSample (N ~ 7 at
    a 33 ms bundle, ~200 at 1 s), so message-level timing measures the BUNDLE
    cadence, not the sample rate. Samples are un-bundled to
    first_sample_time + t_offset_ns before any rate math. The bundle-size
    histogram is reported separately, because a bundle count pinned at a power
    of two (256 / 512 / 2048) is the signature of the producer's OutputRing
    overflowing rather than of a sensor fault.

  * The nominal rate is NOT the true rate. An LSM6DSV trimmed by
    INTERNAL_FREQ_FINE emits 470.6 Hz against a configured 480, and the ego
    camera runs its own PWM-derived grid. Every rate here is measured from the
    data (median inter-sample period); a declared rate, where the file carries
    one, is shown alongside as INFO only and never drives the verdict.

  * Sensor time != write time. Gaps are computed on each message's own capture
    timestamp -- the sensor truth. The MCAP log_time is used only for the
    write-lag column, which separates "the sensor dropped it" from "the writer
    fell behind".

  * A drop is a MISSING TIMESTAMP, so that is what gets counted. Each stream's
    expected grid is fitted from its own timestamps (least squares, seeded from
    the median interval), and a drop is a grid slot no sample landed in --
    counted exactly, not inferred by thresholding an interval. Where a stream
    also carries a producer-side counter (CameraFrameInfo.isp_frame_id) it is
    reported alongside as corroboration only; if the two disagree, both are
    shown, because that disagreement localises the fault. Counting messages and
    dividing by duration would hide a burst loss inside a healthy average, so
    consecutive missing slots are grouped and the worst burst is always shown.

Verdicts are per stream: loss below --warn-loss is OK, above --fail-loss is FAIL.
Non-monotonic or duplicate timestamps are always a FAIL -- they break every
downstream consumer that assumes ordering.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys

import numpy as np

try:
    from mcap.reader import make_reader
    from mcap_protobuf.decoder import DecoderFactory
except ImportError:
    sys.exit("needs: pip install mcap mcap-protobuf-support numpy")

NS = 1_000_000_000

# Schema name -> how to pull sample times out of a decoded message, and what
# kind of stream it is (kinds drive the extra per-family checks below).
CAMERA = "camera"
CAM_INFO = "cam_info"
IMU_RAW = "imu_raw"
IMU_QUAT = "imu_quat"
AUDIO = "audio"
OTHER = "other"


def _ts(t) -> int:
    """google.protobuf.Timestamp -> ns."""
    return t.seconds * NS + t.nanos


def classify(schema_name: str) -> str:
    if schema_name.endswith("CompressedVideo") or schema_name.endswith("RawImage"):
        return CAMERA
    if schema_name.endswith("CameraFrameInfo"):
        return CAM_INFO
    if schema_name.endswith("sensor.ImuRaw"):
        return IMU_RAW
    if schema_name.endswith("geometry_msgs.Quaternion"):
        return IMU_QUAT
    if schema_name.endswith("RawAudio") or schema_name.endswith("AudioCompressed"):
        return AUDIO
    return OTHER


def sample_times(kind: str, msg, log_time: int):
    """Capture times (ns) contributed by one message.

    Returns (times, bundle_size). Everything except ImuRaw contributes exactly
    one time; ImuRaw contributes its whole un-bundled sample run. A message with
    no usable capture timestamp falls back to the MCAP log_time, which is NOT the
    capture instant -- the caller counts those and says so, because a stream
    measured on write times reports the writer's jitter as the sensor's.
    """
    if kind == IMU_RAW:
        base = _ts(msg.first_sample_time)
        return [base + s.t_offset_ns for s in msg.samples], len(msg.samples)
    t = getattr(msg, "timestamp", None)
    if t is not None and hasattr(t, "seconds"):
        ns = _ts(t)
        if ns:
            return [ns], 1
    return [log_time], 0  # 0 = fell back to write time


def local_period(dt, window=101):
    """Rolling median of the interval series -- the period in force at each point.

    Median, not mean, so that the gaps being searched for do not inflate the
    baseline they are measured against. Edges reuse the nearest full window.
    """
    if dt.size <= window:
        return np.full(dt.size, np.median(dt))
    half = window // 2
    win = np.lib.stride_tricks.sliding_window_view(dt, window)
    med = np.median(win, axis=1)
    return np.concatenate([np.full(half, med[0]), med, np.full(dt.size - half - med.size, med[-1])])


def fit_grid(ts, period0, max_resid_frac=0.35):
    """Fit the producer's expected timestamp grid and return each sample's slot.

    A dropped sample IS a missing timestamp, so the honest way to count drops is
    to reconstruct the grid the producer was emitting on and ask which slots came
    up empty. That beats thresholding inter-arrival intervals: it counts a drop
    exactly instead of rounding a stretched interval, it still resolves a single
    drop when jitter smears the boundary, and it distinguishes "one slot holds
    two samples" from "one slot holds none".

    The grid is `t = period * slot + offset`, seeded from the median interval and
    refined by least squares (the true rate is never the nominal one -- an
    LSM6DSV runs 470.6 Hz against a configured 480, so a hard-coded grid would
    manufacture a drop every few seconds).

    Returns None when the samples do not lie on one grid at all -- a stream whose
    rate changes mid-recording, where the caller must fall back to intervals
    rather than report a fit that means nothing.
    """
    if ts.size < 4 or period0 <= 0:
        return None
    idx = np.round((ts - ts[0]) / period0)
    period, offset = period0, ts[0]
    for _ in range(5):
        A = np.vstack([idx, np.ones_like(idx)]).T
        (period, offset), *_ = np.linalg.lstsq(A, ts, rcond=None)
        if period <= 0:
            return None
        new = np.round((ts - offset) / period)
        if np.array_equal(new, idx):
            break
        idx = new
    resid = ts - (period * idx + offset)
    # A grid that does not actually describe the data is worse than no grid.
    if np.abs(resid).max() > max_resid_frac * period:
        return None

    idx = idx.astype(np.int64)
    idx -= idx.min()
    occ = np.bincount(idx, minlength=int(idx.max()) + 1)
    empty = np.flatnonzero(occ == 0)

    # Group consecutive empty slots into bursts, which is how drops actually
    # happen (an I/O stall loses a run, not one isolated frame).
    runs = []
    if empty.size:
        brk = np.flatnonzero(np.diff(empty) > 1)
        for a, b in zip(np.r_[0, brk + 1], np.r_[brk + 1, empty.size]):
            run = empty[a:b]
            runs.append({
                "at_s": float((period * run[0] + offset - ts[0]) / NS),
                "dur_ms": float(period * run.size) / 1e6,
                "missing": int(run.size),
            })
    return {
        "period_ns": float(period),
        "slots": int(occ.size),
        "n_missing": int(empty.size),
        "n_gaps": len(runs),
        "gaps": runs,
        "n_collisions": int((occ - 1)[occ > 1].sum()),
        "resid_ms": float(np.abs(resid).max()) / 1e6,
    }


def analyse(times_ns, log_times_ns, gap_factor: float):
    """Rate / jitter / gap statistics for one stream.

    `times_ns` are capture times in message order -- deliberately NOT sorted, so
    that out-of-order delivery shows up as a defect instead of being silently
    repaired. Gap arithmetic runs on the sorted copy, since one late sample
    would otherwise fake both a huge gap and a huge negative dt.
    """
    t = np.asarray(times_ns, dtype=np.int64)
    n = t.size
    out = {"n": n}
    if n < 3:
        out["error"] = "too few samples to measure a rate"
        return out

    out["n_backwards"] = int((np.diff(t.astype(np.float64)) < 0).sum())

    ts = np.sort(t).astype(np.float64)
    dt_all = np.diff(ts)
    # Exact duplicates carry no timing information, so they are dropped from the
    # cadence maths -- but the interval START TIMES have to be dropped with them,
    # or every gap after the first duplicate is reported at the wrong instant.
    keep = dt_all > 0
    out["n_duplicate"] = int((dt_all == 0).sum())
    dt = dt_all[keep]
    starts = ts[:-1][keep]
    if dt.size < 2:
        out["error"] = "no distinct timestamps"
        return out

    span_s = (ts[-1] - ts[0]) / NS
    period = float(np.median(dt))  # robust to gaps in a way the mean is not
    out.update(
        span_s=span_s,
        # Two different rates, and the difference between them IS the loss:
        # `rate_hz` counts what arrived over the wall clock; `nominal_hz` is the
        # cadence the producer actually ran at, from the typical period.
        rate_hz=(n - 1) / span_s if span_s > 0 else float("nan"),
        nominal_hz=NS / period,
        dt_ms=dict(
            median=period / 1e6,
            mean=float(dt.mean()) / 1e6,
            sd=float(dt.std()) / 1e6,
            min=float(dt.min()) / 1e6,
            p99=float(np.percentile(dt, 99)) / 1e6,
            max=float(dt.max()) / 1e6,
        ),
        jitter_pct=100.0 * float(dt.std()) / period,
    )

    # Rate stability, measured from the LOCAL period rather than by counting
    # samples per wall-clock second: an integer bucket count on a 29.9 Hz stream
    # always alternates 29/30, which is quantisation, not instability.
    loc = local_period(dt)
    out["rate_span"] = dict(
        min_hz=NS / float(loc.max()),
        max_hz=NS / float(loc.min()),
        swing_pct=100.0 * float(loc.max() - loc.min()) / period,
    )

    # Drops are counted as MISSING TIMESTAMPS: fit the grid the producer emitted
    # on, then count the slots nothing arrived in. The interval threshold below
    # is only the fallback for a stream that fits no single grid.
    grid = fit_grid(ts, period)
    if grid is not None:
        out["method"] = "grid"
        out["grid_resid_ms"] = grid["resid_ms"]
        out["n_collisions"] = grid["n_collisions"]
        out.update(
            n_gaps=grid["n_gaps"],
            n_missing=grid["n_missing"],
            loss_pct=100.0 * grid["n_missing"] / max(grid["slots"], 1),
            gaps=grid["gaps"],
            worst_gap_ms=max((g["dur_ms"] for g in grid["gaps"]), default=0.0),
            worst_gap_frames=max((g["missing"] for g in grid["gaps"]), default=0),
        )
    else:
        # No single grid fits, which means the rate itself moves -- an IMU
        # disciplined to the host clock does exactly this. Comparing against one
        # global period would then read the drift as drops, so each interval is
        # judged against the LOCAL period (a rolling median of its neighbours).
        # A missing timestamp still stands out: it doubles the local period.
        out["method"] = "interval"
        local = local_period(dt)
        gap_mask = dt > gap_factor * local
        gap_dt = dt[gap_mask]
        gap_at = starts[gap_mask]
        missing = np.maximum(np.round(gap_dt / local[gap_mask]) - 1, 1).astype(int)
        out.update(
            n_gaps=int(gap_mask.sum()),
            n_missing=int(missing.sum()),
            loss_pct=100.0 * missing.sum() / (n + missing.sum()) if n else 0.0,
            worst_gap_ms=float(gap_dt.max()) / 1e6 if gap_dt.size else 0.0,
            worst_gap_frames=int(missing.max()) if missing.size else 0,
            gaps=[
                {"at_s": float((a - ts[0]) / NS), "dur_ms": float(d) / 1e6, "missing": int(m)}
                for a, d, m in zip(gap_at, gap_dt, missing)
            ],
        )

    # Per-second rate: a stream can lose a steady trickle with no single gap
    # large enough to notice, and that only shows as a sagging bucket rate.
    if span_s >= 2:
        buckets = ((ts - ts[0]) // NS).astype(np.int64)
        counts = np.bincount(buckets)[:-1]  # drop the partial trailing second
        if counts.size:
            out["per_sec_rate"] = dict(
                min=int(counts.min()), median=float(np.median(counts)), max=int(counts.max())
            )

    # log_time - capture_time. A rising write lag means the recorder is falling
    # behind the sensor, which is a different fault from the sensor dropping.
    if log_times_ns is not None:
        lag = (np.asarray(log_times_ns, dtype=np.float64) - t.astype(np.float64)) / 1e6
        if lag.size:
            out["write_lag_ms"] = dict(
                median=float(np.median(lag)), max=float(lag.max()), min=float(lag.min())
            )
    return out


def verdict(st, kind, warn_loss, fail_loss, warn_swing=0.5):
    """Worst-first list of problems; empty means the stream is clean.

    Drops are judged by MISSING TIMESTAMPS -- the empty slots in the producer's
    grid. Where a stream also carries a producer-side sequence counter, that
    counter is reported as CORROBORATION only, never as the verdict: it says what
    the producer believes it emitted, while the timestamps are what a consumer
    can actually use. When the two disagree, both numbers are shown, because the
    disagreement localises the fault rather than resolving it.
    """
    bad = []
    if "error" in st:
        return ["FAIL " + st["error"]]
    if st.get("n_backwards"):
        bad.append(f"FAIL {st['n_backwards']} out-of-order timestamp(s)")
    if st.get("n_duplicate"):
        bad.append(f"FAIL {st['n_duplicate']} duplicate timestamp(s)")
    if st.get("n_collisions"):
        # Distinct times that still land in one slot: not exact duplicates, but
        # the grid says only one sample belongs there.
        bad.append(f"WARN {st['n_collisions']} sample(s) share a grid slot with another")

    loss = st.get("loss_pct", 0.0)
    if loss >= fail_loss:
        bad.append(f"FAIL {loss:.2f}% lost ({st['n_missing']} missing timestamp(s) in "
                   f"{st['n_gaps']} gap(s))")
    elif loss >= warn_loss:
        bad.append(f"WARN {loss:.2f}% lost ({st['n_missing']} missing timestamp(s) in "
                   f"{st['n_gaps']} gap(s))")

    c = st.get("counter")
    if c and c["missing"] != st.get("n_missing"):
        bad.append(f"INFO timestamps say {st.get('n_missing')} missing, the producer's "
                   f"frame counter says {c['missing']} -- they disagree")
    if c and (c["repeats"] or c["backwards"]):
        bad.append(f"FAIL frame counter not unique: {c['repeats']} repeat, "
                   f"{c['backwards']} backwards")
    # Jitter that survives gap removal is a cadence problem, not a loss problem.
    if st.get("jitter_pct", 0) > 50:
        bad.append(f"WARN jitter sd = {st['jitter_pct']:.0f}% of the period")

    # Stability, as distinct from loss: a stream can deliver every sample and
    # still fail to hold its rate.
    r = st.get("rate_span")
    if r and r["swing_pct"] > warn_swing:
        bad.append(f"WARN rate not steady: local rate swings "
                   f"{r['min_hz']:.2f}-{r['max_hz']:.2f} Hz ({r['swing_pct']:.1f}% "
                   f"of the period)")
    return bad


def read_file(path, gap_factor):
    """One pass: every stream's capture times, plus the file's own metadata."""
    streams = {}  # topic -> dict
    meta = {}
    with open(path, "rb") as f:
        reader = make_reader(f, decoder_factories=[DecoderFactory()])
        for m in reader.iter_metadata():
            meta.update(m.metadata)
        for schema, channel, message, decoded in reader.iter_decoded_messages():
            s = streams.get(channel.topic)
            if s is None:
                s = streams[channel.topic] = {
                    "schema": schema.name if schema else "?",
                    "kind": classify(schema.name if schema else ""),
                    "t": [],
                    "log": [],
                    "bundles": [],
                    "fallbacks": 0,
                    "counter": [],
                    "drain": [],
                }
            times, bundle = sample_times(s["kind"], decoded, message.log_time)
            s["t"].extend(times)
            # One log_time per MESSAGE; an un-bundled stream has many samples per
            # message, so the write-lag pairing is only meaningful 1:1.
            if len(times) == 1:
                s["log"].append(message.log_time)
            if s["kind"] == IMU_RAW:
                s["bundles"].append(bundle)
            elif bundle == 0:
                s["fallbacks"] += 1
            # CameraFrameInfo carries a unique per-frame ISP counter, which
            # counts drops EXACTLY instead of inferring them from timing.
            fid = getattr(decoded, "isp_frame_id", None)
            if fid is not None:
                s["counter"].append(int(fid))
                s["drain"].append(int(getattr(decoded, "vi_time_ref", fid)) - int(fid))

    result = {"path": path, "metadata": meta, "streams": {}, "_times": {}}
    for topic, s in sorted(streams.items()):
        result["_times"][topic] = np.asarray(s["t"], dtype=np.int64)
        st = analyse(s["t"], s["log"] if len(s["log"]) == len(s["t"]) else None, gap_factor)
        st["schema"] = s["schema"]
        st["kind"] = s["kind"]
        if s["fallbacks"]:
            st["no_capture_ts"] = s["fallbacks"]
        if s["bundles"]:
            b = np.array(s["bundles"])
            st["bundle"] = dict(
                n_messages=int(b.size),
                min=int(b.min()),
                median=float(np.median(b)),
                max=int(b.max()),
                msg_rate_hz=b.size / st["span_s"] if st.get("span_s") else float("nan"),
            )
        if s["counter"]:
            c = np.array(s["counter"], dtype=np.int64)
            d = np.diff(c)
            st["counter"] = dict(
                first=int(c[0]),
                last=int(c[-1]),
                expected=int(c[-1] - c[0] + 1),
                seen=int(c.size),
                missing=int((c[-1] - c[0] + 1) - c.size),
                repeats=int((d == 0).sum()),
                backwards=int((d < 0).sum()),
            )
            if s["drain"]:
                # vi_time_ref - isp_frame_id: how many frames after capture the
                # ISP stats entry was drained. Constant is healthy; a mixture
                # means entries are being bound to frames inconsistently.
                dr = np.array(s["drain"], dtype=np.int64)
                st["counter"]["drain_frames"] = {
                    int(v): int((dr == v).sum()) for v in np.unique(dr)
                }
        result["streams"][topic] = st
    return result


def stereo_check(result):
    """Inter-camera timing. On a stereo rig the two eyes run one shared grid, so
    the nearest-partner offset should be near-constant; a jump means the pair has
    slipped and every frame after it pairs with the wrong eye."""
    cams = sorted(t for t, s in result["streams"].items() if s["kind"] == CAMERA)
    if len(cams) != 2:
        return None
    a, b = cams
    ta, tb = result["_times"][a], result["_times"][b]
    if ta.size < 3 or tb.size < 3:
        return None
    sa, sb = np.sort(ta), np.sort(tb)
    idx = np.searchsorted(sb, sa)
    idx = np.clip(idx, 1, sb.size - 1)
    left, right = sb[idx - 1], sb[idx]
    nearest = np.where(np.abs(sa - left) <= np.abs(sa - right), left, right)
    off = (sa - nearest) / 1e6
    period = result["streams"][a]["dt_ms"]["median"]
    return {
        "cam_a": a,
        "cam_b": b,
        "offset_ms": dict(
            median=float(np.median(off)), sd=float(off.std()),
            min=float(off.min()), max=float(off.max()),
        ),
        # A frame whose closest partner is more than half a period away has no
        # simultaneous partner at all -- the pair is broken there.
        "unpaired": int((np.abs(off) > period / 2).sum()),
        "n_frames": int(sa.size),
    }


def frameinfo_check(result):
    """Pair each camera with its sibling frame_info stream. The producer emits one
    stats entry per captured frame, so a count mismatch localises the drop: more
    entries than frames means the video encoder lost them after capture, fewer
    means capture itself did."""
    out = []
    for topic, s in result["streams"].items():
        if s["kind"] != CAMERA:
            continue
        sib = result["streams"].get(topic + "/frame_info")
        if not sib:
            continue
        out.append({
            "camera": topic,
            "frames": s["n"],
            "info_entries": sib["n"],
            "delta": sib["n"] - s["n"],
        })
    return out or None


def overlap_check(result, gap_factor):
    """Do the IMU raw and quat streams lose the SAME time windows? They are one
    stream emitted twice (one quat per raw tick), so identical gaps point at the
    single shared publisher, and divergent gaps point at the sensor or the link."""
    raw = [t for t, s in result["streams"].items() if s["kind"] == IMU_RAW and s.get("gaps")]
    quat = [t for t, s in result["streams"].items() if s["kind"] == IMU_QUAT and s.get("gaps")]
    if not raw or not quat:
        return None
    rg = result["streams"][raw[0]]["gaps"]
    qg = result["streams"][quat[0]]["gaps"]
    shared = sum(
        1 for g in rg if any(abs(g["at_s"] - h["at_s"]) < 0.25 for h in qg)
    )
    return {"raw_gaps": len(rg), "quat_gaps": len(qg), "coincident": shared}


def fmt(result, args):
    L = []
    meta = result["metadata"]
    tag = " ".join(
        f"{k}={meta[k]}" for k in ("session_name", "app_version", "hostname", "fps") if k in meta
    )
    L.append(f"\n=== {os.path.basename(result['path'])}"
             f"  ({os.path.getsize(result['path']) / 1e6:.0f} MB)")
    if tag:
        L.append(f"    {tag}")

    hdr = (f"{'topic':<26} {'n':>8} {'rate Hz':>9} {'cadence':>9} {'dt ms':>7} "
           f"{'sd':>6} {'max':>8} {'gaps':>5} {'lost':>7} {'loss%':>7}")
    L.append("    " + hdr)
    L.append("    " + "-" * len(hdr))

    worst = []
    for topic, st in result["streams"].items():
        if st["kind"] == OTHER and not args.all:
            continue
        if "error" in st:
            L.append(f"    {topic:<26} {st['n']:>8}   {st['error']}")
            continue
        d = st["dt_ms"]
        L.append(
            f"    {topic:<26} {st['n']:>8} {st['rate_hz']:>9.2f} {st['nominal_hz']:>9.2f} "
            f"{d['median']:>7.3f} {d['sd']:>6.3f} {d['max']:>8.1f} "
            f"{st['n_gaps']:>5} {st['n_missing']:>7} {st['loss_pct']:>7.2f}"
            + ("" if st.get("method") == "grid" else "  [interval fit]")
        )
        for v in verdict(st, st["kind"], args.warn_loss, args.fail_loss, args.warn_swing):
            L.append(f"        -> {topic}: {v}")
            worst.append(v.split()[0])
        if st.get("no_capture_ts"):
            L.append(f"        -> {topic}: WARN {st['no_capture_ts']} msg(s) had no capture "
                     f"timestamp; timing measured on WRITE time")
            worst.append("WARN")
        if st.get("bundle"):
            b = st["bundle"]
            L.append(f"        .. bundles: {b['n_messages']} msgs at {b['msg_rate_hz']:.1f} Hz, "
                     f"samples/bundle min={b['min']} med={b['median']:.0f} max={b['max']}")
            # kSize-overflow signature: bundles bottoming out at a power of two.
            if b["min"] in (128, 256, 512, 1024, 2048) and b["min"] != b["max"]:
                L.append(f"        -> {topic}: WARN smallest bundle is exactly {b['min']} "
                         f"-- looks like a producer ring overflow, not a sensor drop")
                worst.append("WARN")
        if st.get("counter"):
            c = st["counter"]
            L.append(f"        .. frame counter: {c['seen']}/{c['expected']} seen, "
                     f"{c['missing']} missing, {c['repeats']} repeat, "
                     f"{c['backwards']} backwards")
            if c.get("drain_frames"):
                d = ", ".join(f"{k:+d}:{v}" for k, v in sorted(c["drain_frames"].items()))
                L.append(f"        .. drain latency (vi_time_ref - isp_frame_id): {d}")
        if args.verbose and st.get("method") == "grid":
            L.append(f"        .. grid: {st['nominal_hz']:.3f} Hz, worst sample is "
                     f"{st['grid_resid_ms']:.3f} ms off its slot")
        if st.get("per_sec_rate") and args.verbose:
            p = st["per_sec_rate"]
            L.append(f"        .. per-second count min={p['min']} med={p['median']:.0f} "
                     f"max={p['max']}")
        if st.get("write_lag_ms") and args.verbose:
            w = st["write_lag_ms"]
            L.append(f"        .. write lag ms: med={w['median']:.1f} max={w['max']:.1f}")
        if args.verbose and st.get("gaps"):
            for g in st["gaps"][: args.max_gaps]:
                L.append(f"        .. gap at {g['at_s']:7.2f}s  {g['dur_ms']:8.1f} ms  "
                         f"{g['missing']} missing")
            if len(st["gaps"]) > args.max_gaps:
                L.append(f"        .. ({len(st['gaps']) - args.max_gaps} more gaps)")

    for fi in result.get("frameinfo") or []:
        if fi["delta"]:
            L.append(f"    {fi['camera']}: {fi['frames']} encoded frames vs "
                     f"{fi['info_entries']} frame_info entries ({fi['delta']:+d})")
    if result.get("stereo"):
        s = result["stereo"]
        o = s["offset_ms"]
        L.append(f"    stereo {os.path.basename(s['cam_a'])}<->{os.path.basename(s['cam_b'])}: "
                 f"offset med={o['median']:.2f} ms sd={o['sd']:.2f} "
                 f"range=[{o['min']:.2f},{o['max']:.2f}] unpaired={s['unpaired']}/{s['n_frames']}")
        if s["unpaired"]:
            L.append(f"        -> WARN {s['unpaired']} frame(s) have no partner within half a "
                     f"frame period")
            worst.append("WARN")
    if result.get("imu_overlap"):
        o = result["imu_overlap"]
        L.append(f"    imu raw/quat gaps: raw={o['raw_gaps']} quat={o['quat_gaps']} "
                 f"coincident={o['coincident']} "
                 f"({'one shared publisher' if o['coincident'] else 'independent'})")

    status = "FAIL" if "FAIL" in worst else ("WARN" if "WARN" in worst else "OK")
    result["verdict"] = status
    L.append(f"    VERDICT: {status}")
    return "\n".join(L)


def resolve(specs):
    out = []
    for spec in specs:
        if os.path.isdir(spec):
            out += sorted(glob.glob(os.path.join(spec, "**", "*.mcap"), recursive=True))
        elif any(c in spec for c in "*?["):
            out += sorted(glob.glob(spec))
        else:
            out.append(spec)
    return out


def main():
    p = argparse.ArgumentParser(
        description="framerate stability + dropped-frame check for Visio MCAP recordings",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Four traps")[0],
    )
    p.add_argument("paths", nargs="+", help="mcap file(s), a directory, or a glob")
    p.add_argument("--gap-factor", type=float, default=1.5,
                   help="a dt above this many typical periods counts as a gap (default 1.5)")
    p.add_argument("--warn-loss", type=float, default=0.2, help="WARN at this %% loss (default 0.2)")
    p.add_argument("--fail-loss", type=float, default=1.0, help="FAIL at this %% loss (default 1.0)")
    p.add_argument("--warn-swing", type=float, default=0.5,
                   help="WARN when the local rate swings this %% of a period (default 0.5; "
                        "a healthy ego camera measures 0.01%%, its IMU 0.00-0.05%%)")
    p.add_argument("--all", action="store_true", help="include non-sensor streams too")
    p.add_argument("-v", "--verbose", action="store_true", help="list gaps, per-second rate, lag")
    p.add_argument("--max-gaps", type=int, default=12, help="gaps to list per stream (default 12)")
    p.add_argument("--json", metavar="FILE", help="also write the full report as JSON")
    args = p.parse_args()

    files = resolve(args.paths)
    if not files:
        sys.exit("no mcap files matched")

    reports, worst = [], "OK"
    for path in files:
        try:
            r = read_file(path, args.gap_factor)
        except Exception as e:  # a truncated recording must not kill the batch
            print(f"\n=== {os.path.basename(path)}\n    FAIL unreadable: {e}")
            worst = "FAIL"
            continue
        # The cross-stream checks need the full time arrays; drop them straight
        # after so the report stays JSON-serialisable.
        r["stereo"] = stereo_check(r)
        r["frameinfo"] = frameinfo_check(r)
        r["imu_overlap"] = overlap_check(r, args.gap_factor)
        r.pop("_times", None)
        reports.append(r)

    for r in reports:
        print(fmt(r, args))
        if r["verdict"] == "FAIL":
            worst = "FAIL"
        elif r["verdict"] == "WARN" and worst == "OK":
            worst = "WARN"

    if len(reports) > 1:
        print(f"\n=== {len(reports)} file(s): "
              + ", ".join(f"{os.path.basename(r['path'])}={r['verdict']}" for r in reports))
    print(f"\nOVERALL: {worst}")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(reports, f, indent=2, default=float)
        print(f"wrote {args.json}")
    sys.exit(0 if worst != "FAIL" else 1)


if __name__ == "__main__":
    main()
