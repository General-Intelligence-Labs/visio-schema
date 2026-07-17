# visio-schema Makefile
#
# Targets:
#   make lint      - lint our protos (visio.* namespace; foxglove ignored)
#   make breaking  - check for wire-breaking changes vs main (skipped if
#                    main doesn't exist yet, e.g. before first commit/push)
#   make gen       - lint, then generate Python (full-protobuf) + C++ (nanopb)
#                    bindings IN-PACKAGE (python/visio_schema + cpp/generated_nanopb)
#   make test      - codegen sanity check: import every generated Python module
#   make pytest    - run the Python codec tests (python/tests)
#   make cpp       - build + run the C++ codec tests (cpp/)
#   make wheel     - build the combined visio-schema wheel (gen + codec)
#   make sdist     - build the source distribution (sdist) for PyPI
#   make dist      - build sdist + wheel (PyPI artifacts) into dist/
#   make clean     - remove generated bindings + build artifacts
#
# `buf` is installed locally via `npm install @bufbuild/buf` and lives at
# node_modules/.bin/buf. If you have a system-wide buf in PATH, prefer that.

BUF := $(shell command -v buf 2>/dev/null || echo node_modules/.bin/buf)
PYTHON := $(shell command -v python3 2>/dev/null || echo python)
# Use the VENDORED nanopb generator (third_party/nanopb submodule) rather than
# a system one, so the generated .pb.c always matches the runtime (pb_encode.c
# etc.) the embedded build links. Needs the protobuf python module on $(PYTHON).
NANOPB := $(PYTHON) third_party/nanopb/generator/nanopb_generator.py

# Generated bindings live INSIDE the packages (one wholistic tree per
# language), not in a separate gen/ dir.
PY_PKG  := python
# nanopb C bindings for the embeddable C++ build (RV1106 / HDK) — no full
# libprotobuf. This covers the WHOLE schema, not just the wire Header: the bus
# RELAYS payloads as opaque bytes, but a device ORIGINATES them (camera frames,
# IMU/encoder batches, DeviceInfo, ...) and must encode each type. Per-field
# encoding (zero-copy callback for big blobs, bounded inline for small fields,
# pointer for the once-per-run DeviceInfo Response) lives in proto/nanopb.options.
NANOPB_GEN     := cpp/generated_nanopb
NANOPB_OPTIONS := proto/nanopb.options
# The vendored nanopb ships the well-known protos (timestamp.proto, etc.).
NANOPB_WKT_INC := third_party/nanopb/generator/proto
FOXGLOVE_PROTO := third_party/foxglove-sdk/schemas/proto

.PHONY: lint breaking gen test pytest cpp wheel sdist dist clean help

help:
	@echo "make lint      - lint protos"
	@echo "make breaking  - check for wire-breaking changes vs main"
	@echo "make gen       - lint, then regenerate python/visio_schema + cpp/generated_nanopb bindings"
	@echo "make test      - import every generated Python module (codegen sanity)"
	@echo "make pytest    - run the Python codec tests (python/tests)"
	@echo "make cpp       - build + run the C++ codec tests (cpp/)"
	@echo "make wheel     - build the combined visio-schema wheel (gen + codec)"
	@echo "make sdist     - build the source distribution (sdist)"
	@echo "make dist      - build sdist + wheel (PyPI artifacts) into dist/"
	@echo "make clean     - remove generated bindings + build artifacts"

lint:
	$(BUF) lint proto

# Wire-compat gate: does THIS tree break the contract vs. where it branched off?
# Two constraints the obvious one-liner gets wrong:
#
#  1. buf.yaml is a v2 WORKSPACE whose `foxglove` module is a git SUBMODULE, and
#     buf's `.git#` input carries no submodule content — that module resolves to
#     zero .proto and every `import "foxglove/*.proto"` fails. So the baseline
#     must be a real worktree with the submodule initialised inside it.
#  2. Compare against the MERGE-BASE, not main's tip: a field added on main after
#     this branch diverged reads as a deletion here — a false "you broke the wire".
#
# buf's output is piped through scripts/breaking_waivers.py, which drops ONLY the
# specific, reviewed field deletions enumerated there (e.g. the 0.6.0 IMU slim,
# reserved by number + name) and fails on every other breaking change — so an
# accepted deletion doesn't force relaxing the rule for all future ones.
#
# BREAKING_STRICT=1 turns a missing baseline ref into a failure instead of a
# skip; CI sets it, so the gate can never pass vacuously on a bad checkout.
BREAKING_REF    ?= origin/main
BREAKING_STRICT ?=

breaking:
	@if ! git rev-parse --verify $(BREAKING_REF) >/dev/null 2>&1; then \
		if [ -n "$(BREAKING_STRICT)" ]; then \
			echo "make breaking: '$(BREAKING_REF)' not found and BREAKING_STRICT is set — refusing to pass unchecked"; exit 1; \
		fi; \
		echo "make breaking: skipped (no '$(BREAKING_REF)' ref — fetch it, or set BREAKING_REF=)"; \
		exit 0; \
	fi; \
	base=$$(git merge-base HEAD $(BREAKING_REF)) || exit 1; \
	wt=$$(mktemp -d -t visio-schema-breaking-XXXXXX)/wt; \
	git worktree add --detach "$$wt" "$$base" >/dev/null 2>&1 \
		|| { echo "make breaking: could not check out merge-base $$base"; exit 1; }; \
	git -C "$$wt" submodule update --init --depth 1 third_party/foxglove-sdk >/dev/null 2>&1; \
	if [ -z "$$(ls -A "$$wt/third_party/foxglove-sdk" 2>/dev/null)" ]; then \
		echo "make breaking: foxglove-sdk empty in the baseline worktree — buf would blame your protos"; \
		git worktree remove --force "$$wt" >/dev/null 2>&1; exit 1; \
	fi; \
	echo "buf breaking proto --against merge-base $$(git rev-parse --short $$base)"; \
	out=$$($(BUF) breaking proto --against "$$wt/proto" --error-format json); rc=$$?; \
	: "buf exits 100 = violations found (annotations on stdout); other nonzero = operational error (msg on stderr)"; \
	if [ $$rc -eq 100 ]; then \
		printf '%s\n' "$$out" | $(PYTHON) scripts/breaking_waivers.py; rc=$$?; \
	elif [ $$rc -ne 0 ]; then \
		printf '%s\n' "$$out"; \
	fi; \
	git worktree remove --force "$$wt" >/dev/null 2>&1 || rm -rf "$$wt"; \
	git worktree prune >/dev/null 2>&1; \
	exit $$rc

gen: lint
	# ---- Python: generate into the package tree (python/visio_schema, python/foxglove)
	$(BUF) generate
	# Relocate the generated foxglove.* bindings UNDER visio_schema.foxglove and
	# rewrite their python import paths so they don't collide with the
	# official `foxglove` SDK package. The proto package and descriptor names
	# stay `foxglove.*` (only the python module path changes) so MCAP /
	# Foxglove Studio schema-name matching is unaffected.
	@if [ -d $(PY_PKG)/foxglove ]; then \
	  rm -rf $(PY_PKG)/visio_schema/foxglove; \
	  mkdir -p $(PY_PKG)/visio_schema; \
	  mv $(PY_PKG)/foxglove $(PY_PKG)/visio_schema/foxglove; \
	fi
	@find $(PY_PKG)/visio_schema \( -name '*_pb2.py' -o -name '*_pb2.pyi' \) -print0 \
	  | xargs -0 -r sed -i -E \
	      's/^from foxglove import /from visio_schema.foxglove import /; s/^import foxglove\./import visio_schema.foxglove./'
	# No host full-protobuf C++ output: the only C++ consumer is the embeddable
	# stack, which is nanopb-only. Host-side full-protobuf tooling is Python.
	# ---- C++ (embeddable/nanopb): the WHOLE schema (our protos + the foxglove
	# types our streams use + the WKT timestamp). This is what the RV1106 (HDK)
	# build links — no full libprotobuf. nanopb won't create nested output dirs,
	# so mirror the proto tree first. --error-on-unmatched guards the options
	# file against drift (a renamed field silently losing its bound).
	rm -rf $(NANOPB_GEN); mkdir -p $(NANOPB_GEN)
	@cd proto && find . -type d -exec mkdir -p "$(CURDIR)/$(NANOPB_GEN)/{}" \;
	@cd $(FOXGLOVE_PROTO) && find . -type d -exec mkdir -p "$(CURDIR)/$(NANOPB_GEN)/{}" \;
	@mkdir -p $(NANOPB_GEN)/google/protobuf
	$(NANOPB) -f $(NANOPB_OPTIONS) \
	  --proto-path=proto \
	  --proto-path=$(FOXGLOVE_PROTO) \
	  --proto-path=$(NANOPB_WKT_INC) \
	  --output-dir=$(NANOPB_GEN) \
	  $(shell cd proto && find . -name '*.proto' | sed 's|^\./||') \
	  $(shell cd $(FOXGLOVE_PROTO) && find . -name '*.proto' | sed 's|^\./||') \
	  google/protobuf/timestamp.proto \
	  google/protobuf/duration.proto
	# ---- C++ (embeddable/nanopb): per-payload-type serialized FileDescriptorSet
	# blobs (schema_blobs.gen.hpp). nanopb can't reflect, so the on-device MCAP
	# writer / DeviceInfo announce reads these precomputed bytes. Built host-side
	# where full libprotobuf exists.
	$(PYTHON) scripts/gen_schema_blobs.py $(NANOPB_GEN)

test: gen
	@$(PYTHON) tests/test_imports.py

# Python codec tests. The package tree (python/visio_schema) now holds both the
# generated bindings and the hand-written codec, so no path shim is needed.
pytest: gen
	cd python && $(PYTHON) -m pytest tests -q

# C++ codec tests. nanopb-only — no libprotobuf/abseil install needed.
cpp: gen
	cmake -S cpp -B cpp/build
	cmake --build cpp/build -j
	ctest --test-dir cpp/build --output-on-failure

# One distributable wheel straight from the package tree (generated bindings
# + hand-written codec already live together under python/visio_schema).
wheel: gen
	rm -rf dist
	cd python && $(PYTHON) -m pip wheel --no-deps . -w "$(CURDIR)/dist"
	@echo "wheel written to dist/"

# Source distribution for PyPI. The generated _pb2 (gitignored at HEAD) and the
# native sources are folded in via MANIFEST.in so the sdist builds without a
# codegen toolchain. The platform wheels published to PyPI are the per-version
# manylinux/macOS wheels built by cibuildwheel in CI (.github/workflows/wheels.yml).
sdist: gen
	rm -rf dist
	$(PYTHON) python/_vendor_native.py
	cd python && $(PYTHON) -m build --sdist --outdir "$(CURDIR)/dist"
	@echo "sdist written to dist/"

# Both PyPI artifacts (sdist + a local wheel) into dist/. `python -m build` builds
# the wheel FROM the sdist, exercising the MANIFEST.in vendoring end to end.
dist: gen
	rm -rf dist
	$(PYTHON) python/_vendor_native.py
	cd python && $(PYTHON) -m build --outdir "$(CURDIR)/dist"
	@echo "sdist + wheel written to dist/"

clean:
	rm -rf $(NANOPB_GEN) cpp/build examples/cpp/build dist build
	find $(PY_PKG)/visio_schema \( -name '*_pb2.py' -o -name '*_pb2.pyi' \) -delete
	rm -rf $(PY_PKG)/visio_schema/foxglove
