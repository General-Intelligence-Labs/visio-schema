# visio-schema Makefile
#
# Targets:
#   make lint      - lint our protos (visio.* namespace; foxglove ignored)
#   make breaking  - check for wire-breaking changes vs main (skipped if
#                    main doesn't exist yet, e.g. before first commit/push)
#   make gen       - lint, then generate C++ + Python bindings into gen/
#   make test      - codegen sanity check: import every generated Python module
#   make clean     - remove gen/
#
# `buf` is installed locally via `npm install @bufbuild/buf` and lives at
# node_modules/.bin/buf. If you have a system-wide buf in PATH, prefer that.

BUF := $(shell command -v buf 2>/dev/null || echo node_modules/.bin/buf)
PYTHON := $(shell command -v python3 2>/dev/null || echo python)

.PHONY: lint breaking gen test clean help

help:
	@echo "make lint      - lint protos"
	@echo "make breaking  - check for wire-breaking changes vs main"
	@echo "make gen       - lint, then regenerate gen/cpp + gen/python bindings"
	@echo "make test      - import every generated Python module (codegen sanity)"
	@echo "make clean     - remove gen/"

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

test: gen
	@$(PYTHON) tests/test_imports.py

clean:
	rm -rf gen/
