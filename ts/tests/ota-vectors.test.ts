/**
 * Replay tests/golden/ota_vectors.txt through the TypeScript driver.
 *
 * The third end of the cross-language pin — python/tests/test_ota_vectors.py
 * and cpp/tests/test_ota_vectors.cc replay the same file — so a rule that
 * drifts between the three shows up here as a byte diff rather than on a board.
 *
 * It pins SENDS and the OUTCOME, never the recv call pattern: this driver is
 * async and cannot drain with `recv(0)` the way the Python reference does, and
 * the generator refuses to freeze that difference on purpose.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { relay, Reason, type OtaIo } from '../src/wire/ota';

const VECTORS = fileURLToPath(new URL('../../tests/golden/ota_vectors.txt', import.meta.url));

function loadVectors(): Map<string, Uint8Array> {
  const out = new Map<string, Uint8Array>();
  for (const line of readFileSync(VECTORS, 'utf8').split('\n')) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const eq = t.indexOf('=');
    const hex = t.slice(eq + 1);
    const bytes = new Uint8Array(hex.length / 2);
    for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
    out.set(t.slice(0, eq), bytes);
  }
  return out;
}

const V = loadVectors();
const CASES = [...new Set([...V.keys()].map((k) => k.split('.')[0]!))].sort();
const hex = (b: Uint8Array) => Buffer.from(b).toString('hex');
const pad = (n: number) => String(n).padStart(2, '0');
const text = (k: string) => Buffer.from(V.get(k) ?? new Uint8Array()).toString('utf8');

/** The image is a formula, not a blob — an offset bug cannot hide in zeroes. */
const imageByte = (i: number) => (i * 31 + 7) & 0xff;

test('the golden corpus is present and covers the cases that matter', () => {
  assert.ok(CASES.length > 0, `no vectors loaded from ${VECTORS}`);
  for (const c of ['happy_advert', 'happy_legacy', 'instant_revert', 'wrong_board',
                   'addressed_leaf', 'no_flash']) {
    assert.ok(CASES.includes(c), `the transcript quietly lost the ${c} case`);
  }
});

for (const name of CASES) {
  test(`the driver reproduces the transcript: ${name}`, async () => {
    const p = V.get(`${name}.params`)!;
    const dv = new DataView(p.buffer, p.byteOffset, p.byteLength);
    const total = Number(dv.getBigUint64(0));
    const chunk = dv.getUint32(8);
    const windowBytes = dv.getUint32(12);
    const cap = dv.getUint32(16);
    const sessionId = dv.getBigUint64(20);
    const negotiate = dv.getUint8(29) !== 0;
    const commit = dv.getUint8(30) !== 0;

    let step = 0;
    let now = 0;
    const outbox: Uint8Array[] = [];

    const io: OtaIo = {
      imageBytes: total,
      clock: () => now,
      readImage: (off, len) =>
        Uint8Array.from({ length: len }, (_, i) => imageByte(off + i)),
      send(payload) {
        const want = V.get(`${name}.${pad(step)}.send`);
        assert.ok(want, `${name}: sent ${step + 1} messages, the vector has ${step}`);
        assert.equal(hex(payload), hex(want),
          `${name} step ${step}: the driver emitted a different OtaMessage`);
        for (let i = 0; ; i++) {
          const r = V.get(`${name}.${pad(step)}.reply.${pad(i)}`);
          if (!r) break;
          outbox.push(r);
        }
        step += 1;
        return true;
      },
      async recv(timeoutS) {
        const next = outbox.shift();
        if (next) return next;
        // Virtual time advances only when the driver WAITS, so a 45 s stall
        // costs nothing and a peer that never answers still terminates.
        if (timeoutS) now += timeoutS;
        return null;
      },
    };

    const out = await relay(io, {
      fwVersion: text(`${name}.fw`),
      board: text(`${name}.board`),
      targetDevice: text(`${name}.target`),
      sessionId, window: windowBytes, chunk, chunkCap: cap, negotiate, commit,
    });

    assert.equal(V.get(`${name}.${pad(step)}.send`), undefined,
      `${name}: the vector expects more messages than the driver sent`);

    const o = V.get(`${name}.out`)!;
    const ov = new DataView(o.buffer, o.byteOffset, o.byteLength);
    assert.equal(out.ok ? 1 : 0, ov.getUint8(0), `${name}: ok`);
    assert.equal(out.reason as number, ov.getUint8(1),
      `${name}: reason (${Reason[out.reason]})`);
    assert.equal(out.acked, Number(ov.getBigUint64(2)), `${name}: acked`);
    assert.equal(out.total, Number(ov.getBigUint64(10)), `${name}: total`);
    assert.equal(out.resumes, ov.getUint32(18), `${name}: resumes`);
  });
}
