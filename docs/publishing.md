# Publishing a release to PyPI

`visio-schema` ships to [PyPI](https://pypi.org/project/visio-schema/) as:

- **wheels** — per–Python-version (cp310–cp313) binary wheels for Linux
  (`manylinux_2_28` x86_64) and macOS (`universal2`), each bundling the optional
  native `_creader` reader, built by [cibuildwheel](https://cibuildwheel.pypa.io/), and
- **an sdist** — a source distribution that carries the generated protobuf
  bindings and the native C/C++ sources, so it builds with no codegen toolchain
  (and falls back to the pure-Python reader if there is no compiler).

Everything is driven by [`.github/workflows/wheels.yml`](../.github/workflows/wheels.yml):
pushing a `visio-schema-v*` tag builds + tests all artifacts, publishes them to
PyPI via **Trusted Publishing**, and attaches them to the GitHub release.

## One-time setup: PyPI Trusted Publishing

We use [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC) — no
API token is stored in the repo. Configure it once:

1. On PyPI, create (or claim) the `visio-schema` project. For the very first
   release you can instead add a **pending** publisher (PyPI → *Your projects* →
   *Publishing*) so the project is created on first upload.
2. Add a GitHub Actions trusted publisher with:
   - **Owner**: `General-Intelligence-Labs`
   - **Repository**: `visio-schema`
   - **Workflow**: `wheels.yml`
   - **Environment**: `pypi`
3. In the GitHub repo, create an Environment named **`pypi`**
   (Settings → Environments). Optionally add required reviewers so a release
   waits for manual approval before the upload step runs.

> Prefer an API token instead? Drop the `environment:` + `permissions: id-token`
> from the `publish_pypi` job, add a `PYPI_API_TOKEN` repo secret, and pass
> `with: password: ${{ secrets.PYPI_API_TOKEN }}` to the publish action.

## Cut a release

1. **Bump the version** in [`python/pyproject.toml`](../python/pyproject.toml)
   (`version = "X.Y.Z"`; drop any `.devN` suffix) and move the matching section
   in [`CHANGELOG.md`](../CHANGELOG.md) from *unreleased* to released.
2. **Dry-run the build locally** and sanity-check the artifacts:
   ```bash
   make dist                        # gen + sdist + wheel into dist/
   python -m twine check dist/*
   # optional: upload to TestPyPI first
   #   python -m twine upload --repository testpypi dist/*
   ```
   The generated `_pb2` bindings are gitignored at HEAD; `make dist` regenerates
   and vendors them into the artifacts, so the tag does not need them committed.
3. **Commit, tag, and push** — the tag is what triggers the publish:
   ```bash
   git commit -am "release: visio-schema vX.Y.Z"
   git tag visio-schema-vX.Y.Z
   git push origin main --tags     # push to the General-Intelligence-Labs remote
   ```
4. CI builds + tests the wheels and sdist, then the `publish_pypi` job uploads
   them to PyPI and `release_assets` attaches them to the GitHub release. Watch
   the run under the repo's **Actions** tab.

## Verify

```bash
pip install "visio-schema==X.Y.Z"
python -c "import visio_schema; print(visio_schema.__name__, 'ok')"
visio-display --help
```
