/**
 * Copy the cross-language golden corpus INTO the package, at pack time.
 *
 * The corpus lives at <repo>/tests/golden -- one copy, replayed by the Python
 * reference, the C++ sender and this driver alike, and it must stay there: it
 * is not the TypeScript package's corpus, it is the contract's.
 *
 * But a package cannot ship a file outside its own directory. `files` and the
 * `./golden/*` export both named `golden/` while nothing ever created it, so
 * the packed tarball carried no corpus at all -- declared, empty, and silent,
 * declared, empty, and silent. Consumers take this package straight from the
 * repo, so the corpus has to be staged into it here.
 *
 * Deliberately a copy rather than a symlink: pack follows neither symlinks
 * nor `..`. Restricted to *.txt so that a stray capture or editor backup
 * dropped into the corpus directory cannot ride along into the package.
 */
import { cpSync, existsSync, rmSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const src = fileURLToPath(new URL('../../tests/golden', import.meta.url));
const dst = fileURLToPath(new URL('../golden', import.meta.url));

if (!existsSync(src)) {
  // Packing from a tree with no corpus would ship the same silent hole.
  console.error(`stage-golden: no corpus at ${src}`);
  process.exit(1);
}
rmSync(dst, { recursive: true, force: true });
cpSync(src, dst, {
  recursive: true,
  filter: (p) => statSync(p).isDirectory() || p.endsWith('.txt'),
});
console.log(`stage-golden: ${src} -> ${dst}`);
