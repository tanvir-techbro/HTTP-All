# HTTP-All — top-level entry points.
#
# `make doctor` before anything else, `make conform` before any number is
# published. See README.md and SPEC.md.

SHELL := /bin/bash

# Any directory holding a build.sh is an implementation (SPEC.md 5). Nested
# variants such as Rust/epoll are found by the depth window; harness/, docs/ and
# results/ are excluded.
IMPL_DIRS := $(shell find . -mindepth 2 -maxdepth 3 -name build.sh -type f \
	-not -path './.git/*' -not -path './harness/*' -not -path './docs/*' \
	-not -path './results/*' -printf '%h\n' 2>/dev/null \
	| sed 's|^\./||' | sort)

# `make bench IMPL=C` narrows every target to one implementation.
ifneq ($(strip $(IMPL)),)
TARGET_IMPLS := $(IMPL)
else
TARGET_IMPLS := $(IMPL_DIRS)
endif

.PHONY: help all doctor build conform bench results clean

help:
	@echo 'HTTP-All'
	@echo
	@echo '  make doctor            check the machine (wrk, governor, pinning, toolchains)'
	@echo '  make build             build every implementation'
	@echo '  make conform           byte-exact protocol tests (required before benchmarking)'
	@echo '  make bench             benchmark every implementation'
	@echo '  make results           regenerate results/latest/RESULTS.md + README table'
	@echo '  make clean             remove build outputs (never touches results/)'
	@echo
	@echo 'Add IMPL=<dir> to build, conform, or bench a single implementation,'
	@echo 'e.g. make bench IMPL=C'
	@echo
	@echo 'Implementations found: $(if $(IMPL_DIRS),$(IMPL_DIRS),none)'

all: build conform

doctor:
	@harness/doctor.sh

build:
	@set -e; \
	if [ -z "$(strip $(TARGET_IMPLS))" ]; then echo "no implementations found" >&2; exit 1; fi; \
	for d in $(TARGET_IMPLS); do \
		printf '\n\033[1m==> building %s\033[0m\n' "$$d"; \
		( cd "$$d" && ./build.sh ); \
	done

conform:
	@set -e; failed=""; \
	for d in $(TARGET_IMPLS); do \
		printf '\n\033[1m==> conform: %s\033[0m\n' "$$d"; \
		python3 harness/conformance.py --impl "$$d" || failed="$$failed $$d"; \
	done; \
	if [ -n "$$failed" ]; then \
		printf '\n\033[31mconformance failed:$$failed\033[0m\n' >&2; \
		exit 1; \
	fi; \
	printf '\n\033[32mall implementations conform\033[0m\n'

bench:
	@harness/bench.sh $(if $(strip $(IMPL)),--impl $(IMPL),)

results:
	@test -e results/latest || { echo "no results yet — run 'make bench' first" >&2; exit 1; }
	@python3 harness/report.py --dir results/latest --update-readme

clean:
	@set -e; \
	for d in $(TARGET_IMPLS); do \
		rm -rf "$$d/bin"; \
	done; \
	find . -mindepth 2 -maxdepth 3 -type d -name bin -not -path './.git/*' \
		-not -path './results/*' -exec rm -rf {} + 2>/dev/null || true; \
	echo "cleaned build outputs"
