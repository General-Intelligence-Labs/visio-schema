# Capture clock synchronization

The producer supplies sensor capture time in `Header.timestamp` and in the
sensor payload. Heartbeats and other control messages use send time. Payload
capture times remain in the producer clock; the receiving bus translates data
headers into its own clock. Optical exposure midpoint is outside this contract.

`DeviceInfo` announcements describe registry state at receipt time. A receiving
bus stamps their forwarded envelopes with its local monotonic clock, including
before clock synchronization is ready. The payload's boot identity stays intact.
This keeps announcements in the recorder's timeline without applying a sensor
capture-time mapping to metadata.

Every physical link has one clock authority: the receiving bus attaches a data
producer as a source and estimates that producer's clock. The producer accepts
that receiving bus as a sink and installs its authoritative mapping. A head owns
each wrist link. A host receiving head data owns a separate head-to-host mapping.
There is no independent reciprocal estimator on the same link, offset-only
fallback, protocol negotiation, or legacy clock path.

Heartbeats are hop-local and keyed by the bus's connection ID. Beacons default
to 20 ms. Dense observations let a conservative rate model follow OS frequency
steering without fitting bulk-transfer path delay as oscillator drift. The RTT,
covariance and lease limits below stay unchanged. A bus configured with slower
regular beacons adds 20 ms probes for recently heard sources without usable
acknowledged mappings, or as projected phase sigma approaches 80 us, rate sigma
approaches 8 ppm, or consecutive outliers accumulate. The default cadence does
not duplicate those probes. Silent peers and consumers receive regular beacons;
mapping publication still uses the regular timer. Periodic beacons
carry a process `clock_epoch`; a changed peer epoch resets that link's estimator
and history without reusing an authority revision. Client identity and liveness
remain independent of clock readiness.

## Measurement

The four timestamps are T1 (initiator write), T2 (responder read), T3 (responder
write), T4 (initiator read). The initiating receiver accepts only a response that
matches one of its actual sent T1 values:

```
processing = T3 - T2
round_trip = (T4 - T1) - processing
peer_observation = (T2 + T3) / 2
reference_observation = (T1 + T4) / 2
```

Reads are stamped immediately after the read syscall, before frame decoding or
the bus dispatch mutex. Heartbeat writes use a bounded timed transport lane:
finalize and stamp immediately before the first successful write. An EAGAIN
retry with zero committed bytes gets a new timestamp. A partially committed
COBS frame is completed unchanged before another lane writes. Encoding and
kernel queues remain between these software timestamps and physical USB edges.

A two-state Kalman filter estimates phase and rate with large initial rate
covariance. It rejects negative/over-100-ms RTT, old observations, and clock discontinuities beyond five sigma. Network-plausible innovations
inflate observation noise using the round-trip delay bound. High-RTT samples
are downweighted by their excess delay instead of resetting a learned clock
when the network delay baseline changes. Four persistent
clock discontinuities reacquire. A three-second gap enters holdover; recovery
retains frequency and propagates covariance. A gap beyond 30 seconds reseeds.
The rate process model permits ppm-scale software-clock steering, including
NTP frequency corrections to a continuous monotonic clock.
On Linux, paired monotonic/raw clock reads also measure local OS frequency
steering independently of the network observations. A short, bounded sample
window supplies that measured rate change as an external filter input, retaining
the phase anchor. Each sample uses the narrowest of sixteen bounded read pairs
so cold or preempted ARM reads do not dominate it. Its measurement uncertainty contributes to the rate and
projected phase uncertainty gates. Preempted, sparse or implausible paired
measurements are refused; they do not relax the heartbeat outlier gates or turn
a genuine phase step into a valid rate input. Measurements resuming after a
250 ms gap establish a new input baseline without adding steering that the
network filter may already have learned. All capture/control timestamps
and published mappings remain in their declared monotonic clock domains.
Acquisition requires eight accepted observations, two seconds of span,
phase sigma at most 100 us and rate sigma at most 10 ppm. Covariance describes
statistical uncertainty, not fixed path asymmetry or absolute accuracy.

`[CLOCK]` and `[CLOCK_MAP]` diagnostics distinguish observation rejection,
publication refusal, installation failure and capture-time admission failure.
They include RTT, responder processing, innovation, covariance, active/latest
revision, remaining lease and rejection counts. Prolonged initial acquisition
and failures after acquisition are warnings. Reports are bounded per stage;
alternating valid/invalid packets cannot generate one log per sensor frame.
`[CLOCK_HB]` and `[CLOCK_TIMER]` expose delayed writes and timer gaps. On Linux,
`[CLOCK_REFERENCE]` reports the paired-clock measurement, its uncertainty,
whether it was applied, its age and reasons for unavailable measurements.

## Immutable mapping revisions

The authority publishes one affine `ClockMapping` per link on its regular beacon
timer, independently of whether the latest response happened just before or
after the previous publication:

```
reference = reference_epoch_ns
          + (producer_capture - peer_epoch_ns) * (1 + rate_ppb / 1e9)
```

Activation is 500 ms in the future on the producer clock. Expiry is capped at three
seconds after activation and at three seconds after the last accepted observation. Both ends retain 64 revisions. The producer selects a
revision by capture time and tags outgoing data with its authority epoch and
revision. The receiver resolves that exact revision and checks validity at
capture time; delayed metadata and video therefore use the same transform.
Unknown or expired-at-capture revisions are dropped and increment
`rejected_frames`; raw producer-clock headers cannot leak into a reference-clock
stream. Dynamically announced control/service replies remain deliverable before
clock acquisition, with `source_aligned=false`; recording gates exclude those
unmapped replies. Clock readiness cannot block a command or OTA reply.
Tags are consumed on reception and replaced for
the next physical hop. In-process recording sinks do not create a clock hop.

New segments start at the previous acknowledged segment's extrapolated value.
The estimator's phase discrepancy is removed by rate slew (500 ppm maximum
phase contribution, 1000 ppm maximum combined rate). Revision changes do not
step the capture time reference. The producer acknowledges the active revision
on heartbeats; receipt of tagged data also acknowledges actual use.

The head supplies its reference camera's VI capture phase, period and restart
generation in each mapping. The wrist's capture-phase error and nominal local
period come from the same snapshot used to tag that capture. Exactly one live,
valid reference is required; ambiguous references are refused.

## Shared capture and IDR grid

The bus owns one `SyncService` for clock mappings, capture phase control and IDR
bookkeeping. `HeartbeatService` carries its snapshots and camera status while
retaining peer identity and liveness. Firmware bridges VI/VENC and PWM actuation
to this service; it does not keep another estimator or frame-grid authority.

A follower's selected head consumes a rig capture stream. Its outgoing messages
bypass preview-only congestion thinning, including a keyframe-only latch inherited
from an earlier connection. Other sinks retain their own preview policy; actual
queue limits and transport stall checks still apply. The exemption ends when
that parent is forgotten.

The slowest selected local camera publishes the head's observed capture grid.
Faster cameras use its coincident edges. Grid snapshots include a stable frame
index and periodic IDR cadence. Clock mapping revision updates preserve cadence;
reference epoch or capture restart changes invalidate the old grid. A follower
selects slots only while its capture phase is locked to that reference.

Capture launch also advertises a derived `first_frame_delay_ns` from that slowest
camera. A faster local camera may have one earlier interstitial capture. Its raw
`first_capture_ns` stays visible; `first_shared_capture_ns` identifies the actual
first capture on the common cadence. Launch qualification checks both the early
capture's expected phase and the actual shared capture before recording opens.

Cold recording preparation waits up to eight seconds for usable mappings on
capture-contributing links, independently of the three-second scheduled-wake
budget. An initial AWAKE status cannot bypass clock initialization. Clocks stay
active through standby, so a normal wake does not repeat cold acquisition.
Hardware PREPARE may overlap acquisition, but the authority selects no scheduled
edge until its capture-contributing clocks are usable. A prepared follower can
wait for the head's other clock dependencies within that same bounded clock
budget. An adapter that has not acknowledged PREPARE retains its shorter
hardware timeout. A follower with no selected head is not clock-ready.
Firmware's DeviceReady voice waits for present leaf candidates to finish
discovery and for those clock dependencies to settle. An empty scan must remain
empty for three seconds before permitting standalone readiness, covering the
initial USB gadget re-enumeration gap. Present candidates discover and acquire
their clocks immediately, in parallel with that absence check. This finite grace
does not predict a device connected arbitrarily later. A storage-card re-arm uses
the same readiness check.

For the first fifteen seconds of each reference-owned link, bounded 10 Hz clock
diagnostics retain the actual estimated offset, observed midpoint coordinates,
innovation, RTT and best RTT, as well as covariance and acquisition state. The
logging sink preserves these caller-sampled records without a second per-format
throttle. Healthy steady-state logs return to thirty-second summaries; failure
diagnostics retain their existing bounded burst policy.

`Header.sync_point` identifies a confirmed periodic IDR by authority epoch, grid
generation and frame index. Unlike hop clock tags, it survives timestamp rewriting
and relay. The producer retains the verified input identity by exact capture
timestamp and encoder generation until output, so a later heartbeat or pacing
readiness change cannot erase the identity of that already captured frame.
Producers verify H.265 IDR NAL types 19/20 and supply VPS/SPS/PPS; an
intra-slice flag alone is insufficient. Encoder status counts selected, confirmed
and missed slots. Recording and viewers wait for the regular one-second slots;
start, rotation and recovery do not request additional IDRs.

Firmware selects an IDR immediately before submitting the chosen VI frame. The
current SDK's `SendFrameEx` path on RV1126B drops `enFrameType`, so firmware fences
earlier encoder input with the drain's completed PTS, then uses `RequestIDR` and
`SendFrame`. A fence timeout skips that selection and exposes a missed slot;
it never shifts the request onto an arbitrary later capture.

## Firmware controller and evidence

Capture feedback is posted after VI dequeue, before IDR work or encoder
submission. The PI controller starts at nominal period and uses mapped rate as
feedforward. A soft dead zone removes proportional correction inside 75 us
while preserving integral rate trim. Wrist defaults are kp=0.08, ki=0.0008.
Integral state advances only after successful, unsaturated actuation. Lost or
stale references remove phase correction and hold learned rate; they cannot
report lock. A reference epoch or grid restart resets integral and lock state.

Lock entry requires one continuous second inside period/167. Exit requires
three consecutive violations or one error exceeding twice that window.
This status window is a controller diagnostic, not a launch accuracy threshold.

`[sync] servo.trace=1` enables `/tmp/capture-sync.csv`. A nonblocking
fixed queue feeds a normal-priority writer; the file is capped at 8 MiB, with
queue/file loss counted. It records raw and mapped capture times, reference
revision/grid, error, command, integral, covariance and readiness/lock state.
A PWM sysfs period read is cached commanded state, not a waveform measurement.
Firmware acceptance compares trace errors with recorded capture headers and
checks delay, reconnection, load and thermal stability. Physical exposure
synchronization requires an independent optical/electrical measurement.


## Capture lifecycle

An optional `Heartbeat.capture_sync` advertises native SyncService capture demand,
capabilities and the head-authoritative standby/wake transaction. Authority epoch,
transaction generation and roster generation fence acknowledgements and hardware
actions. Missing capability never implies permission to gate a rig member.
The observed launch grid is carried independently of clock-mapping activation;
it uses the same authority and capture generation as normal frame SyncPoints.
Scheduled launch converts a future head-clock epoch using an installed clock
snapshot valid at the future enable instant. A successor already installed for
that instant can be used before it becomes active at ARM reception. Kernel enable, sensor capture, exposure and admitted IDR timestamps
remain distinct. A completion acknowledges a hardware operation; it does not
claim an observed first frame. The recorder requires verified common IDR evidence.
The hardware adapter runs independently of housekeeping, at 20 ms intervals
during lifecycle transitions (100 ms while settled). Card reads, telemetry and
ISP control work must not consume its arming acknowledgment budget. FSIN
recovery and lifecycle operations share one hardware-action lock.

For a scheduled recording wake, prepare the writer before its demand releases
the cameras. Bind its start to frame zero of the upcoming head grid generation.
When arming the future edge, the head advertises that generation and its planned
first shared capture phase, allowing wrists to select the same IDR before their
first encoder input. The first measured reference capture replaces the planned
phase without advancing frame zero. Individual follower cameras may select
their actual frame-zero encoder inputs before the other local cameras dequeue.
A planned grid does not establish launch
readiness: all required measured captures and the complete independently
decodable IDR group must pass before acknowledging or announcing recording.
An already awake rig continues to use its next regular IDR slot.
A recorder joining a wake already in ARM also selects a future regular slot:
another consumer may have released frame zero before that writer was prepared.
The first group's transport arrivals use the recording-start deadline, not the
short steady-state rotation arrival window. Capture-time tolerance, common
SyncPoint identity, selected membership and staging-memory bounds still apply.
Local cadences may differ by 2:1 while sharing the grid origin; the head's period
must not replace a follower's selected capture period.

`SetCapturePreset.preset_id` is a persisted selection from the board's advertised
`DeviceState.capture_settings.supported_presets`. The state distinguishes selected
and active IDs, applied per-camera rates and an application error. Selection is
applied at restart. Resolution and bitrate remain independent operations.
