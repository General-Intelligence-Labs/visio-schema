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
import { test } from 'node:test';

import { loadGolden } from './golden.js';

import { abortMessage, listMessage, readMessage } from '../src/wire/diag.js';


const V = loadGolden('diag_wire_vectors.txt');
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
