# Assignment 1 -- Mutex locks, sense-reversing barrier, concurrent counters.
# Works with Apple clang (macOS, arm64) and g++/clang++ (Linux). No external dependency.
#
#   make            build tests and benchmarks
#   make test       run all correctness tests
#   make tsan       run the tests under ThreadSanitizer (smaller sizes)
#   make mutation   build the deliberately broken variants and show that the tests catch them
#   make check      all of the above: build, test, tsan, mutation; logs -> results/<host>/
#   make full       the whole workflow: check, bench, figures, report, zip  (bench alone: about an hour)
#   make bench      run the full measurement campaign -> results/<host>/*.csv   (run on an idle machine)
#   make figures    draw every figure of the report from results/           (needs matplotlib)
#   make report     build report/report.pdf                                 (needs xelatex)
#   make zip        pack the submission
CXX ?= c++
ifeq ($(origin CXX),default)
CXX := c++
endif
CXXFLAGS ?= -std=c++20 -O2 -Wall -Wextra -pthread
CPPFLAGS += -Iinclude
PYTHON   ?= python3
BUILD    := build
SHELL    := /bin/bash

# Name of this machine in results/: the short hostname, as the benchmark programs print it. Choose
# another one with `make check HOST=my-machine`; `make bench` uses the same name.
HOST ?= $(or $(PC_HOST),$(shell hostname | cut -d. -f1))
LOGS := results/$(HOST)

# OpenMP baselines are compiled only when the compiler accepts -fopenmp (g++ on Linux; Apple clang
# needs Homebrew libomp). The code is under #ifdef _OPENMP.
OPENMP_OK := $(shell echo 'int main(){}' | $(CXX) -x c++ -fopenmp - -o /dev/null 2>/dev/null && echo yes)
ifeq ($(OPENMP_OK),yes)
CXXFLAGS += -fopenmp
endif

HDRS := $(wildcard include/*.h include/*.hpp)
TEST_SRCS  := $(wildcard tests/*.cpp)
BENCH_SRCS := $(wildcard bench/*.cpp)
TESTS   := $(patsubst tests/%.cpp,$(BUILD)/%,$(TEST_SRCS))
BENCHES := $(patsubst bench/%.cpp,$(BUILD)/%,$(BENCH_SRCS))

all: $(TESTS) $(BENCHES)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%: tests/%.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ $<

$(BUILD)/%: bench/%.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ $<

# ThreadSanitizer builds of the tests.
$(BUILD)/%_tsan: tests/%.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -O1 -g -fsanitize=thread -o $@ $<

# Part-specific targets (test, tsan, mutation for each part) live in mk/*.mk so that the three
# parts can be developed independently. Each fragment appends to TEST_TARGETS, TSAN_TARGETS and
# MUTATION_TARGETS.
TEST_TARGETS :=
TSAN_TARGETS :=
MUTATION_TARGETS :=
-include mk/*.mk

test: $(TEST_TARGETS)
tsan: $(TSAN_TARGETS)
mutation: $(MUTATION_TARGETS)

bench: all
	$(PYTHON) scripts/run_bench.py --host $(HOST) $(BENCH_ARGS)

# The whole workflow in order. Each step is a separate make call so that `make -j` cannot run two
# steps at once (a benchmark must run alone). Before the campaign, wait up to 10 minutes for the
# load left by the tests to fall. `check` also keeps the output of the test, TSan and mutation runs in
# results/<host>/ (test.log, tsan.log, mutation.log, and the log of every mutant in mutation_logs/).
check:
	$(MAKE) all
	mkdir -p $(LOGS)
	set -o pipefail; PC_HOST=$(HOST) $(MAKE) --no-print-directory test 2>&1 | tee $(LOGS)/test.log
	set -o pipefail; PC_HOST=$(HOST) $(MAKE) --no-print-directory tsan 2>&1 | tee $(LOGS)/tsan.log
	set -o pipefail; PC_HOST=$(HOST) $(MAKE) --no-print-directory mutation 2>&1 | tee $(LOGS)/mutation.log
	rm -rf $(LOGS)/mutation_logs && mkdir -p $(LOGS)/mutation_logs
	cp $(BUILD)/*mut*.log $(LOGS)/mutation_logs/

full:
	$(MAKE) check
	$(MAKE) bench BENCH_ARGS="--wait 10"
	$(MAKE) figures
	$(MAKE) report
	$(MAKE) zip

figures:
	$(PYTHON) scripts/plot.py

report: 
	cd report && latexmk -xelatex -interaction=nonstopmode -halt-on-error report.tex

clean:
	rm -rf $(BUILD)

.PHONY: all test tsan mutation bench check full figures report clean zip
