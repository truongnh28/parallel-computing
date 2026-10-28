# Assignment 1 -- Mutex locks, sense-reversing barrier, concurrent counters.
# Works with Apple clang (macOS, arm64) and g++/clang++ (Linux). No external dependency.
#
#   make            build tests and benchmarks
#   make test       run all correctness tests
#   make tsan       run the tests under ThreadSanitizer (smaller sizes)
#   make mutation   build the deliberately broken variants and show that the tests catch them
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
	$(PYTHON) scripts/run_bench.py

figures:
	$(PYTHON) scripts/plot.py

report: 
	cd report && latexmk -xelatex -interaction=nonstopmode -halt-on-error report.tex

clean:
	rm -rf $(BUILD)

.PHONY: all test tsan mutation bench figures report clean zip
