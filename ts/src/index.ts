/**
 * @gilabs/visio-schema — the stable public API.
 *
 * The twin of `visio_schema/__init__.py`, and it carries the same contract: the
 * names re-exported HERE, plus the generated proto schema under `./gen/*`, are
 * what downstream consumers may depend on. Everything reachable through a
 * submodule path is advanced/internal — still importable, not covered by the
 * guarantee, and free to change without a major bump.
 *
 * The facade is deliberately EMPTY until the modules it would name have
 * settled. Freezing a surface before there is anything to freeze buys a pin
 * that only ever describes the last thing added; `python/tests/test_public_api.py`
 * is the shape to copy once `wire/` lands, including its co-update ritual —
 * facade, pin, AGENTS.md table, CHANGELOG, version, together.
 */

export {};
