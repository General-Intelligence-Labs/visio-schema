# Capture clock synchronization

The producer supplies sensor capture time in `Header.timestamp` and in the
sensor payload. Heartbeats and other control messages use send time. Payload
capture times remain in the producer clock; the receiving bus translates data
headers into its own clock. Optical exposure midpoint is outside this contract.

Every physical link has one clock authority: the receiving bus attaches a data
producer as a source and estimates that producer's clock. The producer accepts
that receiving bus as a sink and installs its authoritative mapping. A head owns
each wrist link. A host receiving head data owns a separate head-to-host mapping.
There is no independent reciprocal estimator on the same link, offset-only
fallback, protocol negotiation, or legacy clock path.

Heartbeats are hop-local and keyed by the bus's connection ID. Periodic beacons
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
retains frequency and propagates covariance. A gap beyond 30 seconds reseeds. Acquisition requires eight accepted observations, five seconds of span,
phase sigma at most 100 us and rate sigma at most 10 ppm. Covariance describes
statistical uncertainty, not fixed path asymmetry or absolute accuracy.

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

`[exposure_sync] trace_enable=1` enables `/tmp/capture-sync.csv`. A nonblocking
fixed queue feeds a normal-priority writer; the file is capped at 8 MiB, with
queue/file loss counted. It records raw and mapped capture times, reference
revision/grid, error, command, integral, covariance and readiness/lock state.
A PWM sysfs period read is cached commanded state, not a waveform measurement.
Firmware acceptance compares trace errors with recorded capture headers and
checks delay, reconnection, load and thermal stability. Physical exposure
synchronization requires an independent optical/electrical measurement.
