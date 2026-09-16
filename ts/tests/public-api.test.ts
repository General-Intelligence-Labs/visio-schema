/**
 * Pin the stable public API — the curated facade plus the package's entry points.
 *
 * The TypeScript twin of `python/tests/test_public_api.py`, and it carries the
 * same contract: the names re-exported from `src/index.ts` and the subpaths in
 * the `exports` map are what consumers depend on. Removing, renaming, or
 * silently WIDENING the surface is a breaking change. Submodule internals are
 * deliberately not pinned and may change freely.
 *
 * If a change is intentional, update TOGETHER: the facade, `FACADE_API` below,
 * the `exports` map, CHANGELOG.md, and the version.
 *
 * Widening is pinned as hard as removal, for the reason Python states: a
 * surface that grows silently is one nobody decided on, and every accidental
 * export becomes a compatibility obligation the moment someone imports it.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import * as facade from '../src/index.js';

/** The frozen facade: exactly these names, no more, no fewer. */
const FACADE_API = new Set([
  // wire/ota — entry point + the vocabulary to read its answer
  'relay',
  'Reason',
  'STREAM_OTA',
  'QUIESCE_RULES',
  'QUIESCE_COMMAND_ID',
  // routing/topics — the topic grammar
  'boardRootOfTopic',
  'boardRootOrNull',
  'boardRootOfTopics',
  'cameraIndexOfTopic',
  'rootSuffix',
  'sideOfTopics',
]);

/** The subpaths the `exports` map promises. `./gen/*` and `./golden/*` are
 *  wildcards over generated/fixture trees, pinned by verify-pack.mjs instead. */
const PUBLIC_SUBPATHS = new Set(['.', './wire/ota', './wire/diag', './routing/topics', './gen/*', './golden/*']);

const pkg = JSON.parse(
  readFileSync(fileURLToPath(new URL('../package.json', import.meta.url)), 'utf8'),
);

test('the facade is exactly the frozen set', () => {
  // Runtime values only: `export type` leaves nothing at runtime, so the type
  // exports are covered by the build (they either resolve or tsc fails).
  const actual = new Set(Object.keys(facade));
  assert.deepEqual([...actual].sort(), [...FACADE_API].sort());
});

test('every facade name is actually defined', () => {
  for (const name of FACADE_API) {
    assert.notEqual((facade as Record<string, unknown>)[name], undefined, name);
  }
});

test('the exports map is exactly the promised subpath set', () => {
  assert.deepEqual(Object.keys(pkg.exports).sort(), [...PUBLIC_SUBPATHS].sort());
});

test('the version and the pin were updated together', () => {
  // Not a version check — a REMINDER made mechanical: the facade is frozen as
  // of the version named in src/index.ts, so the two must agree.
  const src = readFileSync(fileURLToPath(new URL('../src/index.ts', import.meta.url)), 'utf8');
  const frozenAt = /FROZEN as of ([\d.]+)/.exec(src)?.[1];
  assert.equal(frozenAt, pkg.version,
    'src/index.ts says the facade is frozen at a different version than package.json — ' +
    'the co-update ritual was half-done');
});

test('the drivers are reachable by subpath, and say why they are not flattened', () => {
  // The design note is load-bearing: both drivers define `nextSessionId` and
  // `STALL_TIMEOUT_S`, so flattening would force a rename.
  assert.ok(pkg.exports['./wire/ota'] && pkg.exports['./wire/diag']);
  assert.ok(!FACADE_API.has('nextSessionId'), 'ambiguous between the two drivers');
  assert.ok(!FACADE_API.has('STALL_TIMEOUT_S'), 'ambiguous between the two drivers');
});
