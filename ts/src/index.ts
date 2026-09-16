/**
 * @gilabs/visio-schema — the stable public API.
 *
 * The twin of `visio_schema/__init__.py`, and it carries the same contract: the
 * names re-exported HERE, plus the generated proto schema under `./gen/*`, are
 * what downstream consumers may depend on. Everything reachable through a
 * submodule path is advanced/internal — still importable, not covered by the
 * guarantee, and free to change without a major bump.
 *
 * It is NOT frozen yet. `python/tests/test_public_api.py` is the shape to copy
 * once the remaining modules land, including its co-update ritual — facade,
 * pin, AGENTS.md table, CHANGELOG, version, together. Pinning a surface that is
 * still growing buys a test that only ever describes the last thing added.
 */

export {
  // The OTA driver. `relay` is the entry point; the rest is the vocabulary a
  // caller needs to read its answer or drive its seams.
  relay,
  negotiateChunk,
  negotiatedChunkBytes,
  nextSessionId,
  bundleError,
  Reason,
  STREAM_OTA,
  QUIESCE_RULES,
  QUIESCE_COMMAND_ID,
  type OtaIo,
  type OtaOptions,
  type Outcome,
  type Progress,
} from './wire/ota.js';
