# visio-schema Makefile
#
# Targets:
#   make lint      - lint our protos (visio.* namespace; foxglove ignored)
#   make breaking  - check for wire-breaking changes vs main (skipped if
#                    main doesn't exist yet, e.g. before first commit/push)
#   make gen       - lint, then generate C++ + Python bindings into gen/
#   make test      - codegen sanity check: import every generated Python module
#   make pytest    - run the Python codec tests (python/tests)
#   make cpp       - build + run the C++ codec tests (cpp/)
#   make wheel     - build the combined visio-schema wheel (gen + codec)
#   make clean     - remove gen/ and build artifacts
#
# `buf` is installed locally via `npm install @bufbuild/buf` and lives at
# node_modules/.bin/buf. If you have a system-wide buf in PATH, prefer that.

BUF := $(shell command -v buf 2>/dev/null || echo node_modules/.bin/buf)
PYTHON := $(shell command -v python3 2>/dev/null || echo python)
PROTOC := $(shell command -v protoc 2>/dev/null)

PROTO_PATHS := -I proto -I third_party/foxglove-sdk/schemas/proto
PROTO_FILES := $(shell find proto -name '*.proto') $(shell find third_party/foxglove-sdk/schemas/proto -name '*.proto')

.PHONY: lint breaking gen test pytest cpp wheel clean help

help:
	@echo "make lint      - lint protos"
	@echo "make breaking  - check for wire-breaking changes vs main"
	@echo "make gen       - lint, then regenerate gen/cpp + gen/python bindings"
	@echo "make test      - import every generated Python module (codegen sanity)"
	@echo "make pytest    - run the Python codec tests (python/tests)"
	@echo "make cpp       - build + run the C++ codec tests (cpp/)"
	@echo "make wheel     - build the combined visio-schema wheel (gen + codec)"
	@echo "make clean     - remove gen/ and build artifacts"

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
	$(BUF) generate
	@if [ -z "$(PROTOC)" ]; then \
	  echo "make gen: protoc not found in PATH; skipping C++ codegen"; \
	else \
	  mkdir -p gen/cpp; \
	  $(PROTOC) $(PROTO_PATHS) --cpp_out=gen/cpp $(PROTO_FILES); \
	fi

test: gen
	@$(PYTHON) tests/test_imports.py

# Python codec tests. conftest.py puts gen/python on sys.path so the
# generated bindings + hand-written codec resolve as one `visio` package.
pytest: gen
	cd python && $(PYTHON) -m pytest tests -q

# C++ codec tests. Needs a protobuf CONFIG install (conda/system/vcpkg).
cpp: gen
	cmake -S cpp -B cpp/build
	cmake --build cpp/build -j
	ctest --test-dir cpp/build --output-on-failure

# Build the single distributable wheel: one `visio` import root combining
# the hand-written codec (python/visio) and the generated bindings
# (gen/python/visio), staged into a temp tree so neither source dir is
# polluted.
wheel: gen
	rm -rf build/wheel
	mkdir -p build/wheel/visio
	cp -r python/. build/wheel/
	cp -r gen/python/visio/. build/wheel/visio/
	cd build/wheel && $(PYTHON) -m pip wheel --no-deps . -w "$(CURDIR)/dist"
	@echo "wheel written to dist/"

clean:
	rm -rf gen/ build/ dist/ cpp/build/ examples/cpp/build/
