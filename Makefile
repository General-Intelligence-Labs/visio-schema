# visio-schema Makefile
#
# Targets:
#   make lint      - lint our protos (visio.* namespace; foxglove ignored)
#   make breaking  - check for wire-breaking changes vs main (skipped if
#                    main doesn't exist yet, e.g. before first commit/push)
#   make gen       - lint, then generate C++ + Python bindings IN-PACKAGE
#                    (python/visio_schema + cpp/generated)
#   make test      - codegen sanity check: import every generated Python module
#   make pytest    - run the Python codec tests (python/tests)
#   make cpp       - build + run the C++ codec tests (cpp/)
#   make wheel     - build the combined visio-schema wheel (gen + codec)
#   make clean     - remove generated bindings + build artifacts
#
# `buf` is installed locally via `npm install @bufbuild/buf` and lives at
# node_modules/.bin/buf. If you have a system-wide buf in PATH, prefer that.

BUF := $(shell command -v buf 2>/dev/null || echo node_modules/.bin/buf)
PYTHON := $(shell command -v python3 2>/dev/null || echo python)
PROTOC := $(shell command -v protoc 2>/dev/null)

PROTO_PATHS := -I proto -I third_party/foxglove-sdk/schemas/proto
PROTO_FILES := $(shell find proto -name '*.proto') $(shell find third_party/foxglove-sdk/schemas/proto -name '*.proto')

# Generated bindings live INSIDE the packages (one wholistic tree per
# language), not in a separate gen/ dir.
PY_PKG  := python
CPP_GEN := cpp/generated

.PHONY: lint breaking gen test pytest cpp wheel clean help

help:
	@echo "make lint      - lint protos"
	@echo "make breaking  - check for wire-breaking changes vs main"
	@echo "make gen       - lint, then regenerate python/visio_schema + cpp/generated bindings"
	@echo "make test      - import every generated Python module (codegen sanity)"
	@echo "make pytest    - run the Python codec tests (python/tests)"
	@echo "make cpp       - build + run the C++ codec tests (cpp/)"
	@echo "make wheel     - build the combined visio-schema wheel (gen + codec)"
	@echo "make clean     - remove generated bindings + build artifacts"

lint:
	$(BUF) lint proto

breaking:
	@if git rev-parse --verify origin/main >/dev/null 2>&1 \
	     || git rev-parse --verify main    >/dev/null 2>&1; then \
		$(BUF) breaking proto --against '.git#branch=main,subdir=proto'; \
	else \
		echo "make breaking: skipped (no 'main' ref yet — first commit pending)"; \
	fi

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
	# ---- C++: generate into cpp/generated via local protoc
	@if [ -z "$(PROTOC)" ]; then \
	  echo "make gen: protoc not found in PATH; skipping C++ codegen"; \
	else \
	  rm -rf $(CPP_GEN); mkdir -p $(CPP_GEN); \
	  $(PROTOC) $(PROTO_PATHS) --cpp_out=$(CPP_GEN) $(PROTO_FILES); \
	fi

test: gen
	@$(PYTHON) tests/test_imports.py

# Python codec tests. The package tree (python/visio_schema) now holds both the
# generated bindings and the hand-written codec, so no path shim is needed.
pytest: gen
	cd python && $(PYTHON) -m pytest tests -q

# C++ codec tests. Needs a protobuf CONFIG install (conda/system/vcpkg).
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

clean:
	rm -rf $(CPP_GEN) cpp/build examples/cpp/build dist build
	find $(PY_PKG)/visio_schema \( -name '*_pb2.py' -o -name '*_pb2.pyi' \) -delete
	rm -rf $(PY_PKG)/visio_schema/foxglove
