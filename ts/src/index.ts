/**
 * @gilabs/visio-schema — the stable public API.
 *
 * The twin of `visio_schema/__init__.py`, carrying the same contract: the names
 * re-exported HERE, plus the generated proto schema under `./gen/*`, are what
 * consumers may depend on. Everything reachable through a submodule path is
 * advanced/internal — still importable, not covered by the guarantee, and free
 * to change without a major bump.
 *
 * FROZEN as of 0.10.0, pinned by `tests/public-api.test.ts`. Changing it means
 * updating, TOGETHER: this file, `FACADE_API` in that test, the `exports` map in
 * package.json, CHANGELOG.md, and the version — the same co-update ritual
 * `python/tests/test_public_api.py` documents. The version moved to 0.10.0 here
 * because this release removed four names and added six; pre-1.0 a breaking
 * change bumps MINOR (docs/protocol/versioning.md).
 *
 * The test can only enforce the SELF-CONSISTENT half — that this file, the
 * facade set and the version agree. CHANGELOG.md is a human step, and no test
 * can tell a real entry from a placeholder.
 *
 * WHY THE DRIVERS ARE NOT FLATTENED HERE: `wire/ota` and `wire/diag` each
 * define `nextSessionId` and `STALL_TIMEOUT_S`, with different values. Only
 * those two collide — `listFiles`/`readFile`/`DiagError` would flatten fine —
 * but a facade carrying one driver whole and the other in part is a worse
 * contract than one that carries neither. Both are reached at
 * `@gilabs/visio-schema/wire/ota` and `/wire/diag`, which is how Python reaches
 * them too: its facade re-exports neither driver.
 *
 * `relay` is the exception and is deliberate: it is the OTA entry point, and
 * naming it here is what makes the OTA vocabulary below (Reason, Outcome …)
 * mean anything.
 */

export {
  // The OTA driver's entry point and the vocabulary to read its answer. The
  // rest of its surface — seams, tuning constants — is at ./wire/ota.
  relay,
  Reason,
  STREAM_OTA,
  QUIESCE_RULES,
  QUIESCE_COMMAND_ID,
  type OtaIo,
  type OtaOptions,
  type Outcome,
  type Progress,
} from './wire/ota.js';

export {
  // The topic grammar (docs/protocol/stream_type_map.md §"Topic convention").
  // Pure string handling, no collisions, and the thing every consumer of a
  // DeviceInfo announce needs before it can attribute a frame to a board.
  boardRootOfTopic,
  boardRootOrNull,
  boardRootOfTopics,
  cameraIndexOfTopic,
  rootSuffix,
  sideOfTopics,
} from './routing/topics.js';
