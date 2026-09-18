/**
 * The golden corpora, in the `key=hex` grammar every language replays.
 *
 * One loader, because there are three of these suites now (ota, diag,
 * recordings) and each had its own copy — the Python and C++ sides solved this
 * once each (`python/tests/_golden.py`, `cpp/tests/golden_vectors_test_util.hpp`).
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/** `<corpus>.txt` from tests/golden, as `key -> raw bytes`. */
export function loadGolden(name: string): Map<string, Uint8Array> {
  const path = fileURLToPath(new URL(`../../tests/golden/${name}`, import.meta.url));
  const out = new Map<string, Uint8Array>();
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const eq = t.indexOf('=');
    if (eq < 0) continue;
    const hex = t.slice(eq + 1);
    const bytes = new Uint8Array(hex.length / 2);
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
    out.set(t.slice(0, eq), bytes);
  }
  return out;
}
