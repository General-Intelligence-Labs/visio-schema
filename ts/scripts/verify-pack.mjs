/**
 * Load the BUILT package under plain node, the way a consumer does.
 *
 * Everything else here tests the TypeScript sources through tsx, which resolves
 * extensionless relative imports. node does not. So the build could be -- and
 * for its whole life was -- unloadable while `make ts-test` stayed green and
 * `tsc` reported nothing: `moduleResolution: bundler` permits a bare
 * `./wire/ota`, and protoc-gen-es was emitting the same. Nothing imported
 * dist/ until a consumer resolved this package out of the repo.
 *
 * So this walks the `exports` map and imports every target it names. Nothing is
 * listed by hand: a hand-written probe cannot notice an export it was never
 * told about, and an unchecked export is the same declared-but-empty hole in a
 * new place. Both conditions are checked -- a build that emits .js but no .d.ts
 * loads fine here and breaks the consumer's type-check instead.
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const root = new URL('../', import.meta.url);
const abs = (rel) => fileURLToPath(new URL(rel, root));
const pkg = JSON.parse(readFileSync(new URL('package.json', root), 'utf8'));

let failed = 0;
const fail = (m) => { console.error(`  FAIL ${m}`); failed += 1; };
const pass = (m) => console.log(`  ok   ${m}`);

/** Every string target in an export entry, paired with its condition. */
const targetsOf = (entry) =>
  typeof entry === 'string'
    ? [['default', entry]]
    : Object.entries(entry).filter(([, v]) => typeof v === 'string');

/** Files under `dir` (recursively), as paths relative to the package root. */
function* walk(dir) {
  if (!existsSync(abs(dir))) return;
  for (const name of readdirSync(abs(dir))) {
    const rel = `${dir}/${name}`;
    yield* statSync(abs(rel)).isDirectory() ? walk(rel) : [rel];
  }
}

/** Every real file a `./a/*.js` pattern expands to. */
const expand = (pattern) => {
  const [head, tail] = pattern.split('*');
  return [...walk(head.replace(/\/$/, ''))].filter((f) => f.endsWith(tail));
};

/** -> true when the target is usable. Imports .js; existence-checks the rest. */
async function probe(rel) {
  if (!rel.endsWith('.js')) {
    if (existsSync(abs(rel))) return true;
    fail(`${rel} does not exist`);
    return false;
  }
  try {
    await import(new URL(rel, root).href);
    return true;
  } catch (e) {
    // The MESSAGE, never just e.code: the bug this exists to catch reports as a
    // bare ERR_MODULE_NOT_FOUND, while the message names the unresolved
    // specifier AND the file importing it -- the only two facts that locate it.
    fail(`${rel}: ${e?.message ?? e}`);
    return false;
  }
}

for (const [subpath, entry] of Object.entries(pkg.exports)) {
  const targets = targetsOf(entry);
  if (targets.length === 0) { fail(`${subpath} names no string target`); continue; }

  for (const [condition, target] of targets) {
    const label = `${subpath} (${condition})`;
    if (!target.includes('*')) {
      if (await probe(target)) pass(`${label} -> ${target}`);
      continue;
    }
    // EVERY member, not a sample: a broken relative import in a binding no
    // sample happens to pull in ships unnoticed otherwise.
    const members = expand(target);
    if (members.length === 0) { fail(`${label} -> ${target} matches no file`); continue; }
    let good = 0;
    for (const m of members) if (await probe(m)) good += 1;
    if (good === members.length) pass(`${label} -> ${target} (${good} files)`);
  }
}

// `files` is load-bearing and statically present: if it disappears, npm falls
// back to .npmignore rules and packs a different set. Assert rather than
// default, so that regression cannot iterate zero times and report success.
if (!Array.isArray(pkg.files)) {
  fail('package.json has no `files` array');
} else {
  for (const f of pkg.files) if (!existsSync(abs(f))) fail(`files[] names "${f}", which does not exist`);
  // ...and the check that actually matters, which is the CONVERSE: every path
  // `exports` points at must sit under something `files` ships. An export whose
  // tree is missing from `files` resolves here (the working tree has it) and is
  // absent from the consumer's install -- declared, empty, and silent again.
  const shipped = pkg.files.map((f) => f.replace(/^\.\//, '').replace(/\/$/, ''));
  for (const [subpath, entry] of Object.entries(pkg.exports)) {
    for (const [, target] of targetsOf(entry)) {
      const top = target.replace(/^\.\//, '').split('/')[0];
      if (!shipped.includes(top)) fail(`exports["${subpath}"] -> ${target}, but files[] does not ship "${top}"`);
    }
  }
}

if (failed) { console.error(`verify-pack: ${failed} failure(s)`); process.exit(1); }
console.log('verify-pack: the built package loads');
