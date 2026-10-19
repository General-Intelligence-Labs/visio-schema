# Receiver clock clients

Use the native bus's `HeartbeatService` for receiving device data. The source
attachment owns the clock estimate; the producer installs its immutable mapping
and tags frames. Do not add an application offset estimator or independently
shift capture timestamps. The bus rewrites data `Header.timestamp` and leaves
payload timestamps in their original sensor clock.

See [the protocol](protocol/timesync.md) for the four software timestamps,
Kalman acquisition, revision selection, uncertainty limits and diagnostics.
Python's `bus.timesync()` exposes readiness, phase/rate sigma, sample age,
minimum RTT, mapping epoch/revision and rejected frame count. Readiness is
separate from heartbeat liveness and from measured application accuracy.
