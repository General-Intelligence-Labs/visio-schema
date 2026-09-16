/**
 * What the OTA driver DOES — the rules, as opposed to `ota-vectors.test.ts`,
 * which only says all three implementations agree on them.
 *
 * This is the TypeScript end of what `python/tests/test_wire_ota.py` covers,
 * and it exists because the golden corpus reaches four Reason codes out of
 * eleven and **not one of its nine cases exercises a resume** (every `out`
 * record has resumes=0; the generator defines a `gap_once` device policy that
 * no case uses). The rewind, the resume bound and the soft retry are the most
 * intricate part of a windowed transfer and the likeliest thing to be wrong in
 * a fresh port — and nothing was checking them.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { relay, Reason, type OtaIo, type Outcome } from '../src/wire/ota';

const TOTAL = 10_000;
const CHUNK = 1_000;

const enum S { RECEIVING = 1, STAGED = 3, SUCCESS = 5, FAILED = 6, NEEDS_RESUME = 7 }

/** OtaStatus{session_id=2, state=3, bytes_received=4, error_code=6}, hand-encoded
 *  so the fake never depends on the encoder under test. */
function status(state: S, acked: number, errorCode?: string): Uint8Array {
  const b: number[] = [0x18, state, 0x20];
  let v = acked;
  do { const x = v & 0x7f; v >>>= 7; b.push(v ? x | 0x80 : x); } while (v);
  if (errorCode !== undefined) {
    const bytes = [...Buffer.from(errorCode, 'utf8')];
    b.push(0x32, bytes.length, ...bytes);
  }
  return new Uint8Array(b);
}

const isChunk = (p: Uint8Array) => p.some((b, i) => b === 0x5a && i < 12);
const isCommit = (p: Uint8Array) => p.some((b, i) => b === 0x62 && i < 12);
const isAbort = (p: Uint8Array) => p.some((b, i) => b === 0x6a && i < 12);

interface Rig {
  io: OtaIo;
  sent: Uint8Array[];
  aborts: number;
  now(): number;
}

/** `onChunk` decides what the device says back to each chunk. */
function rig(onChunk: (n: number, acked: number, push: (s: Uint8Array) => void) => void): Rig {
  let now = 0;
  let acked = 0;
  let chunks = 0;
  const outbox: Uint8Array[] = [];
  const sent: Uint8Array[] = [];
  const r = {
    sent,
    aborts: 0,
    now: () => now,
    io: {
      imageBytes: TOTAL,
      clock: () => now,
      readImage: (_o: number, n: number) => new Uint8Array(n),
      send(p: Uint8Array) {
        sent.push(p);
        if (isAbort(p)) r.aborts += 1;
        if (isChunk(p)) {
          chunks += 1;
          acked += CHUNK;
          onChunk(chunks, acked, (s) => outbox.push(s));
        }
        if (isCommit(p)) outbox.push(status(S.STAGED, TOTAL));
        return true;
      },
      async recv(timeoutS: number) {
        const n = outbox.shift();
        if (n) return n;
        if (timeoutS) now += timeoutS;   // virtual time: only advances on a wait
        return null;
      },
    } as OtaIo,
  };
  return r;
}

const OPTS = { fwVersion: '1.0.0', board: 'b', chunk: CHUNK, negotiate: false };
const run = (r: Rig, extra = {}): Promise<Outcome> => relay(r.io, { ...OPTS, ...extra });

test('a NEEDS_RESUME rewinds to the device watermark and finishes', async () => {
  // The device says it only has 3 KiB when we have sent 5; the driver must
  // resend from THERE, not carry on from its own cursor.
  let fired = false;
  const r = rig((n, acked, push) => {
    if (n === 5 && !fired) {
      fired = true;
      push(status(S.NEEDS_RESUME, 3 * CHUNK));
    } else {
      push(status(S.RECEIVING, acked));
    }
  });
  const out = await run(r);
  assert.ok(out.ok, out.detail);
  assert.equal(out.resumes, 1, 'the rewind must be counted');
});

test('a device that never stops asking for a rewind is given up on', async () => {
  // Otherwise a board stuck at one offset holds the link forever.
  const r = rig((_n, _acked, push) => push(status(S.NEEDS_RESUME, 0)));
  const out = await run(r, { maxResumes: 3 });
  assert.equal(out.ok, false);
  assert.equal(out.reason, Reason.FailTooLossy);
  assert.ok(out.resumes > 3);
  assert.equal(r.aborts, 1, 'the device must be told, not just dropped');
});

test('no acks at all is a stall, not a hang', async () => {
  const r = rig(() => { /* silence */ });
  const out = await run(r, { stallTimeoutS: 5, softRetryS: 1e9 });
  assert.equal(out.ok, false);
  assert.equal(out.reason, Reason.FailStalled);
  assert.equal(r.aborts, 1);
});

test('a quiet tail is re-driven before the stall timer fires', async () => {
  // The soft retry exists so a lost tail costs a resend, not the whole push.
  let acks = 0;
  const r = rig((n, acked, push) => {
    if (n <= 2) { acks += 1; push(status(S.RECEIVING, acked)); }
    // after that: silence, until the soft retry re-drives from the watermark
  });
  const out = await run(r, { stallTimeoutS: 30, softRetryS: 2 });
  assert.equal(out.ok, false, 'this device never completes; the point is HOW it fails');
  assert.equal(out.reason, Reason.FailStalled);
  assert.ok(out.resumes > 0, 'the soft retry must have re-driven at least once');
  assert.ok(acks > 0);
});

test('a trickle that never finishes still hits the overall deadline', async () => {
  // The stall timer alone cannot catch a device acking just inside it forever.
  const r = rig((_n, acked, push) => push(status(S.RECEIVING, Math.min(acked, CHUNK))));
  const out = await run(r, { deadlineS: 20, stallTimeoutS: 1e9, softRetryS: 1e9, window: CHUNK * 2 });
  assert.equal(out.ok, false);
  assert.equal(out.reason, Reason.FailDeadline);
  assert.equal(r.aborts, 1);
});

test('SUCCESS at commit is reported as SUCCESS, not STAGED', async () => {
  const r = rig((_n, acked, push) => push(status(S.RECEIVING, acked)));
  const io = r.io as OtaIo & { send: (p: Uint8Array) => boolean };
  const inner = io.send.bind(io);
  const outbox: Uint8Array[] = [];
  io.send = (p: Uint8Array) => {
    if (isCommit(p)) { outbox.push(status(S.SUCCESS, TOTAL)); return true; }
    return inner(p);
  };
  const origRecv = io.recv.bind(io);
  io.recv = async (t: number) => outbox.shift() ?? origRecv(t);
  const out = await relay(io, OPTS);
  assert.ok(out.ok);
  assert.equal(out.reason, Reason.OkSuccess);
});

test('a commit whose STAGED races the reboot is still a success', async () => {
  // The device reboots to apply and its ack routinely loses that race; the
  // caller's post-reboot version check is the real proof, so the driver must
  // not call this a failure.
  const r = rig((_n, acked, push) => push(status(S.RECEIVING, acked)));
  const io = r.io as OtaIo & { send: (p: Uint8Array) => boolean };
  const inner = io.send.bind(io);
  io.send = (p: Uint8Array) => (isCommit(p) ? true : inner(p));  // commit acked by nothing
  const out = await relay(io, { ...OPTS, commitWaitS: 2 });
  assert.ok(out.ok, out.detail);
  assert.equal(out.reason, Reason.OkCommittedUnconfirmed);
});

test('a link that dies AFTER the commit is a success, before it is not', async () => {
  const dying = (dieOn: (p: Uint8Array) => boolean) => {
    const r = rig((_n, acked, push) => push(status(S.RECEIVING, acked)));
    const io = r.io as OtaIo & { send: (p: Uint8Array) => boolean };
    const inner = io.send.bind(io);
    io.send = (p: Uint8Array) => { if (dieOn(p)) throw new Error('EPIPE'); return inner(p); };
    return io;
  };
  const after = await relay(dying((p) => isCommit(p) && false), { ...OPTS, commitWaitS: 1 });
  assert.ok(after.ok);

  const before = await relay(dying(isChunk), OPTS);
  assert.equal(before.ok, false);
  assert.equal(before.reason, Reason.FailLinkDropped);
});

test('a FAILED with a real error code stops the push', async () => {
  const r = rig((n, acked, push) =>
    push(n === 3 ? status(S.FAILED, acked, 'busy_recording') : status(S.RECEIVING, acked)));
  const out = await run(r);
  assert.equal(out.ok, false);
  assert.equal(out.reason, Reason.FailDevice);
  assert.match(out.detail, /busy_recording/);
});

test('a post-stage no_session FAILED is ignored, any other is not', async () => {
  // After staging the device has no session while it reboots, so the last
  // in-flight chunks bounce back as no_session — treating those as real would
  // turn every successful push into a failure.
  const benign = rig((n, acked, push) => {
    push(status(S.RECEIVING, acked));
    if (n === 4) { push(status(S.STAGED, TOTAL)); push(status(S.FAILED, TOTAL, 'no_session')); }
  });
  const ok = await run(benign);
  assert.ok(ok.ok, ok.detail);

  const real = rig((n, acked, push) => {
    push(status(S.RECEIVING, acked));
    if (n === 4) { push(status(S.STAGED, TOTAL)); push(status(S.FAILED, TOTAL, 'revert_failed')); }
  });
  const bad = await run(real);
  assert.equal(bad.ok, false, 'revert_failed after a stage is real');
});

test('a status stamped for another session is not folded', async () => {
  // Its bytes_received would move our watermark and its FAILED would abort a
  // healthy transfer. Session 0 IS folded — older firmware never sets it.
  const foreign = new Uint8Array([0x10, 0x63, 0x18, S.FAILED, 0x32, 3, 0x62, 0x61, 0x64]);
  const r = rig((n, acked, push) => {
    if (n === 2) push(foreign);
    push(status(S.RECEIVING, acked));
  });
  const out = await run(r, { sessionId: 0x99n });
  assert.ok(out.ok, `a foreign session's FAILED aborted the push: ${out.detail}`);
});

test('the in-flight window is respected', async () => {
  // With a non-blocking transport this is the ONLY backpressure: exceed it and
  // a CDC-ACM gadget's RX FIFO overruns and the image arrives corrupt.
  let maxInFlight = 0;
  let acked = 0;
  const r = rig((n, a, push) => { acked = a; push(status(S.RECEIVING, a)); });
  const io = r.io as OtaIo & { send: (p: Uint8Array) => boolean };
  const inner = io.send.bind(io);
  let sentBytes = 0;
  io.send = (p: Uint8Array) => {
    if (isChunk(p)) { sentBytes += CHUNK; maxInFlight = Math.max(maxInFlight, sentBytes - acked); }
    return inner(p);
  };
  const out = await relay(io, { ...OPTS, window: 3 * CHUNK });
  assert.ok(out.ok, out.detail);
  assert.ok(maxInFlight <= 3 * CHUNK, `in-flight reached ${maxInFlight}, window was ${3 * CHUNK}`);
});
