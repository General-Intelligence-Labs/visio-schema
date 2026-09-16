/**
 * Replay tests/golden/diag_wire_vectors.txt through the TypeScript encoder.
 *
 * The cross-language half of the diag port: `diag.test.ts` proves this driver
 * is self-consistent, and this proves it agrees with the Python reference and
 * with anything else that reads the same corpus. Without it the port rests on
 * a differential run nobody re-does.
 *
 * NOTE this is not diag_vectors.txt, which pins the VDLG/VDLW log containers
 * from mcap/crypto.py — a different layer that this driver never opens.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { abortMessage, listMessage, readMessage } from '../src/wire/diag.js';

const VECTORS = fileURLToPath(new URL('../../tests/golden/diag_wire_vectors.txt', import.meta.url));

function loadVectors(): Map<string, Uint8Array> {
  const out = new Map<string, Uint8Array>();
  for (const line of readFileSync(VECTORS, 'utf8').split('\n')) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const eq = t.indexOf('=');
    const hex = t.slice(eq + 1);
    const bytes = new Uint8Array(hex.length / 2);
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
    out.set(t.slice(0, eq), bytes);
  }
  return out;
}

const V = loadVectors();
const SID = 0x51d0n;

/** Every case in the file, and the call that must reproduce it. */
const CASES: Record<string, () => Uint8Array> = {
  list: () => listMessage({ sessionId: SID }),
  list_addressed: () => listMessage({ sessionId: SID, targetDevice: 'GILABS-A' }),
  abort: () => abortMessage({ sessionId: SID }),
  abort_addressed: () => abortMessage({ sessionId: SID, targetDevice: 'GILABS-A' }),
  read: () => readMessage('a.vdlg', 4096n, 1024, { sessionId: SID }),
  read_to_end: () => readMessage('b.vdlg', 0n, 0, { sessionId: SID }),
  read_addressed: () => readMessage('c.vdlg', 1n, 2, { sessionId: SID, targetDevice: 'GILABS-B' }),
};

test('the corpus is present and every case is covered', () => {
  const inFile = [...V.keys()].map((k) => k.replace(/\.msg$/, '')).sort();
  assert.deepEqual(inFile, Object.keys(CASES).sort(),
    'the corpus and this replay disagree about which cases exist — regenerate ' +
    'with scripts/gen_diag_wire_vectors.py, or add the missing call here');
});

for (const [name, call] of Object.entries(CASES)) {
  test(`${name} encodes exactly as the reference does`, () => {
    const expected = V.get(`${name}.msg`);
    assert.ok(expected, `${name}.msg missing from the corpus`);
    assert.deepEqual([...call()], [...expected]);
  });
}
