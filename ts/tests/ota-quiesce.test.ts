/**
 * The quiesce half of the OTA contract (docs/protocol/ota.md §6).
 *
 * Deliberately separate from the vector replay, because the transcript CANNOT
 * see this: it pins OtaMessage frames, and a quiesce is a Command on another
 * stream. §6 says so outright, and it is why the per-language suites are the
 * pin — python/tests/test_wire_ota.py and cpp/tests/test_wire_ota_quiesce.cc
 * are the other two. Without this file the TS driver would pass every check
 * with the hook never called and the restore never firing.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  relay, Reason, QUIESCE_RULES, QUIESCE_COMMAND_ID, type OtaIo,
} from '../src/wire/ota.js';

const TOTAL = 10_000;
const CHUNK = 1_000;

/** A limb that acks every chunk contiguously and STAGEs at commit. */
function rig(opts: { ack?: boolean; dieAfter?: number } = {}) {
  const calls: boolean[] = [];
  const order: string[] = [];
  let now = 0;
  let acked = 0;
  let sends = 0;
  const outbox: Uint8Array[] = [];

  const status = (state: number) => {
    // OtaStatus{ session_id=2, state=3, bytes_received=4 } — hand-encoded so the
    // fake stays independent of the encoder under test.
    const b: number[] = [0x18, state];
    let v = acked;
    b.push(0x20);
    do { const x = v & 0x7f; v >>>= 7; b.push(v ? x | 0x80 : x); } while (v);
    return new Uint8Array(b);
  };

  const io: OtaIo = {
    imageBytes: TOTAL,
    clock: () => now,
    readImage: (_o, n) => new Uint8Array(n),
    send(payload) {
      if (opts.dieAfter !== undefined && sends >= opts.dieAfter) return false;
      sends += 1;
      // tag 0x5a = field 11 (chunk), 0x62 = field 12 (commit), 0x72 = query
      if (payload.includes(0x72) && sends === 1) order.push('query');
      if (payload.some((b, i) => b === 0x52 && i < 12)) order.push('begin');
      if (payload.some((b, i) => b === 0x5a && i < 12)) {
        acked += CHUNK;
        outbox.push(status(1)); // RECEIVING
      }
      if (payload.some((b, i) => b === 0x62 && i < 12)) outbox.push(status(3)); // STAGED
      return true;
    },
    async recv(timeoutS) {
      const n = outbox.shift();
      if (n) return n;
      if (timeoutS) now += timeoutS;
      return null;
    },
    quiesce(quiet) {
      calls.push(quiet);
      order.push('quiesce');
      return opts.ack ?? true;
    },
  };
  return { io, calls, order };
}

const OPTS = { fwVersion: '1.0.0', board: 'test_board', chunk: CHUNK, negotiate: false };

test('quiets the link before the begin and restores it at the end', async () => {
  const r = rig();
  const out = await relay(r.io, OPTS);
  assert.ok(out.ok, out.detail);
  assert.deepEqual(r.calls, [true, false], 'quiet on entry, restore on exit, once each');
});

test('restores even when the transfer fails', async () => {
  // The whole reason the restore is in a `finally`: relay returns from a dozen
  // places, and a link left dark is hardest to explain on the failures. On a
  // leg that outlives the transfer — a rig head's to a limb — a policy left
  // behind keeps that limb's cameras dark until something reconnects it.
  const r = rig({ dieAfter: 2 });
  const out = await relay(r.io, OPTS);
  assert.equal(out.ok, false);
  assert.equal(out.reason, Reason.FailLinkDropped);
  assert.deepEqual(r.calls, [true, false]);
});

test('a device that never acked the quiesce is not restored', async () => {
  // Nothing was applied, so there is nothing to put back — and a policy
  // REPLACES the link's previous one, so undoing one that was never
  // established would clear whatever the device legitimately had.
  const r = rig({ ack: false });
  await relay(r.io, OPTS);
  assert.deepEqual(r.calls, [true]);
});

test('the quiesce precedes the chunk negotiation', async () => {
  // The OtaQuery's answer crosses the same link the video is saturating, so a
  // query that times out under load silently costs the transfer its negotiated
  // chunk size.
  const r = rig();
  await relay(r.io, { ...OPTS, negotiate: true });
  assert.equal(r.order[0], 'quiesce');
  assert.equal(r.order[1], 'query');
});

test('an unset hook pushes against a live link and sends no policy', async () => {
  // `--no-pause-video` is a documented bench verb: measuring a push against a
  // loaded link on purpose must stay expressible.
  const r = rig();
  const io = { ...r.io };
  delete (io as { quiesce?: unknown }).quiesce;
  const out = await relay(io, OPTS);
  assert.ok(out.ok, out.detail);
  assert.equal(r.calls.length, 0);
});

test('the constants match the Python and C++ twins', () => {
  // Nothing else holds the three in step on this: the transcript cannot carry a
  // Command, so a drifted 0xB15 would surface only as "the device never acked",
  // and only on one language.
  assert.deepEqual([...QUIESCE_RULES], ['**/camera/*']);
  assert.equal(QUIESCE_COMMAND_ID, 0xb15);
});
