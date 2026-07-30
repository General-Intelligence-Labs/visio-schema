# `mcap-rate-check` — framerate stability and dropped samples in a recording

Answers two questions about any Visio MCAP: **did the stream hold its rate**, and
**did anything go missing**. Covers the camera streams, the per-frame exposure
sidecar, the IMU fusion output and the bundled raw IMU, plus audio and any other
timestamped stream it finds.

It reads the schemas embedded in the file, so it needs no `visio_schema` install
and works on a recording from any schema version — including one produced by
firmware newer than this checkout.

```
pip install mcap mcap-protobuf-support numpy
python mcap_rate_check.py <file|dir|glob> [-v] [--json out.json]
```

```
=== ego_0000.mcap  (274 MB)
    session_name=session_00023-... app_version=dev hostname=GILABS-o5yf1fhK fps=30
    topic                             n   rate Hz   cadence   dt ms     sd      max  gaps    lost   loss%
    -----------------------------------------------------------------------------------------------------
    /ego/audio/0                   7847     62.50     62.50  16.000  0.000     16.0     0       0    0.00
    /ego/camera/0                  3750     29.90     29.90  33.441  0.015     33.6     0       0    0.00
    /ego/camera/0/frame_info       3754     29.90     29.90  33.441  0.015     33.6     0       0    0.00
    /ego/imu/0/quat               59024    470.24    470.37   2.126  0.035      8.5     8      10    0.02
    /ego/imu/0/raw                59016    470.24    470.37   2.126  0.035      8.5     8      10    0.02
    stereo 0<->1: offset med=0.01 ms sd=0.00 unpaired=0/3750
    VERDICT: OK
```

Exit status is 0 unless some stream FAILs, so it drops into a shell gate.

## How a drop is counted

**A drop is a missing timestamp.** Two mechanisms, chosen per stream:

- **Grid fit** — the expected grid `t = period·slot + offset` is fitted from the
  stream's own timestamps by least squares, and a drop is a slot nothing landed
  in. Counted exactly, not inferred from a threshold. The ego camera fits to
  within 0.4 ms on a 33.44 ms period, so this is unambiguous.
- **Local-period intervals** — the fallback when no single grid fits, which is
  the honest answer for a clock-disciplined IMU whose rate genuinely wanders.
  Comparing such a stream against one global period would read the drift as
  drops, so each interval is judged against a rolling median of its neighbours.
  These rows are marked `[interval fit]`.

Consecutive missing slots are grouped into bursts, because that is how loss
actually happens — an I/O stall loses a run, not one isolated frame.

## Traps it gets right

Each of these manufactures a fake fault if missed.

- **`/imu/<i>/raw` is BUNDLED.** One `ImuRaw` message carries N `ImuRawSample`
  (~7 at a 33 ms bundle, ~200 at 1 s), so message-level timing measures the
  *bundle* cadence, not the sample rate. Samples are un-bundled to
  `first_sample_time + t_offset_ns` before any rate maths. The bundle-size
  histogram is reported separately: a bundle count pinned at a power of two
  (256/512/2048) is the signature of the producer's `OutputRing` overflowing,
  not of a sensor fault.
- **The nominal rate is not the true rate.** An LSM6DSV trimmed by
  `INTERNAL_FREQ_FINE` emits ~470.6 Hz against a configured 480, and the ego
  camera runs its own PWM-derived grid. Every rate here is measured from the
  data; a declared rate is shown as context and never drives the verdict.
- **Sensor time is not write time.** Gaps are computed on each message's own
  capture timestamp. The MCAP `log_time` feeds only the write-lag column, which
  separates "the sensor dropped it" from "the writer fell behind".
- **A producer counter is corroboration, not the verdict.** Where a stream
  carries one (`CameraFrameInfo.isp_frame_id`) it is reported alongside; if it
  disagrees with the timestamps, both numbers are shown, because the
  disagreement localises the fault rather than resolving it.

## Cross-stream checks

- **Stereo pairing** — nearest-partner offset between the two eyes, and how many
  frames have no partner within half a frame period. A co-phased ego holds
  ~0.01 ms; a jump to a full frame period means the pair has slipped.
- **Camera vs `frame_info`** — one stats entry per captured frame, so a count
  mismatch localises where a frame was lost.
- **IMU raw vs quat** — the two are one stream emitted twice, so identical gap
  windows point at the single shared publisher rather than at the sensor.

## Reference numbers (healthy ego)

Useful for telling "unusual" from "broken":

| Stream | Rate | Cadence | Local-rate swing |
|---|---|---|---|
| camera 0/1 | 29.90 Hz | 33.440–33.441 ms | 0.01 % |
| imu raw/quat | 470.15 Hz | 2.127 ms | 0.00–0.05 % |
| audio | 62.50 Hz | 16.000 ms | 0.00 % |
| stereo offset | — | 0.01 ms median | — |

`--warn-swing` defaults to 0.5 %, which sits an order of magnitude above every
healthy figure above and well below a real excursion (a board showing a clock
step measured 1.4 %).

## Options

| Flag | Effect |
|---|---|
| `-v` | list every gap, the grid residual, per-second counts and write lag |
| `--json FILE` | full report as JSON, for trend-tracking across sessions |
| `--all` | include non-sensor streams (`/device_info`, `/ego/system_health`, …) |
| `--warn-loss` / `--fail-loss` | loss thresholds in percent (default 0.2 / 1.0) |
| `--warn-swing` | rate-stability threshold (default 0.5 % of a period) |
| `--gap-factor` | interval-fallback gap threshold in periods (default 1.5) |

## Reading a recording off a device

Copy over **MTP**, not `adb pull` — sustained adb I/O perturbs the very rates
being measured:

```
/run/user/1000/gvfs/mtp:host=rockchip_<hostname>_<adbserial>/SD Card/data/
```
