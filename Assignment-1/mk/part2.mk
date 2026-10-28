# Part 2 -- barrier synchronization: test, ThreadSanitizer and mutation targets.
# Included by the top-level Makefile (which defines CXX, CPPFLAGS, CXXFLAGS, BUILD and HDRS).

TEST_TARGETS     += test-part2
TSAN_TARGETS     += tsan-part2
MUTATION_TARGETS += mutation-part2

.PHONY: test-part2 tsan-part2 tsan-part2-false-positive mutation-part2

# Correctness test of every barrier: one PASS/FAIL line per variant, non-zero exit on failure.
# It runs twice: the plain build (exactly the production code), then the schedule-fuzzing build
# (random delays at the test points inside the sense-reversing barrier).
test-part2: $(BUILD)/test_barrier $(BUILD)/test_barrier_fuzz
	$(BUILD)/test_barrier
	$(BUILD)/test_barrier_fuzz

$(BUILD)/test_barrier_fuzz: tests/test_barrier.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -DPC_BARRIER_FUZZ -o $@ $<

# The same test under ThreadSanitizer with reduced sizes. A ThreadSanitizer report makes the
# program exit with a non-zero code, so this target fails on any data race.
tsan-part2: $(BUILD)/test_barrier_tsan
	$(BUILD)/test_barrier_tsan small

# Not part of `make tsan`: shows the FALSE ThreadSanitizer report for a barrier whose
# implementation is inside an uninstrumented library (libc++ std::barrier on macOS).
tsan-part2-false-positive: $(BUILD)/test_barrier_tsan
	-TSAN_OPTIONS="abort_on_error=0 $$TSAN_OPTIONS" $(BUILD)/test_barrier_tsan small impl=std_barrier tsan_uninstrumented

# ---- mutation testing ----------------------------------------------------------------------------
# tests/test_barrier.cpp compiled with -DPC_MUTANT=k tests one deliberately broken barrier
# (the list is in that file). Every mutant is built three times: plain, with schedule fuzzing
# (-DPC_BARRIER_FUZZ) and with ThreadSanitizer.
PART2_MUTANTS  := 1 2 3 4 5
PART2_MUT_BINS := $(foreach m,$(PART2_MUTANTS),$(BUILD)/barrier_mut$(m))
PART2_MUT_FUZZ := $(foreach m,$(PART2_MUTANTS),$(BUILD)/barrier_mut$(m)_fuzz)
PART2_MUT_TSAN := $(foreach m,$(PART2_MUTANTS),$(BUILD)/barrier_mut$(m)_tsan)
# How often each stress test is repeated for every mutant (hitting a race is a matter of chance).
PART2_MUT_RUNS ?= 5

$(PART2_MUT_BINS): $(BUILD)/barrier_mut%: tests/test_barrier.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -DPC_MUTANT=$* -o $@ $<

$(PART2_MUT_FUZZ): $(BUILD)/barrier_mut%_fuzz: tests/test_barrier.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -DPC_MUTANT=$* -DPC_BARRIER_FUZZ -o $@ $<

$(PART2_MUT_TSAN): $(BUILD)/barrier_mut%_tsan: tests/test_barrier.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -O1 -g -fsanitize=thread -DPC_MUTANT=$* -o $@ $<

# Runs every mutant and prints one table row per mutant.
#   stress columns:   "caught c/N (wrong w, hang h)": in c of N runs the test rejected the mutant,
#                     w times because a check failed (exit code 1: wrong episode or wrong value),
#                     h times because the watchdog found a hang (exit code 2).
#   ThreadSanitizer:  "data race" if the log contains a ThreadSanitizer report, and the verdict
#                     of the ordinary checks in that (much slower) build.
# Logs are kept in $(BUILD)/barrier_mut*.log. The target fails if a mutant is caught by nothing.
mutation-part2: $(PART2_MUT_BINS) $(PART2_MUT_FUZZ) $(PART2_MUT_TSAN)
	@echo "== Part 2 mutation testing: $(PART2_MUT_RUNS) runs per stress column, 1 ThreadSanitizer run =="
	@printf '%-23s | %-29s | %-29s | %s\n' "mutant" "plain stress test" "stress + schedule fuzzing" "ThreadSanitizer build"
	@stress() { \
	  wrong=0; hang=0; other=0; r=1; \
	  while [ $$r -le $(PART2_MUT_RUNS) ]; do \
	    $$1 stall_s=2 cap_s=0.5 > $$1.run$$r.log 2>&1; \
	    case $$? in 0) ;; 1) wrong=$$((wrong+1));; 2) hang=$$((hang+1));; *) other=$$((other+1));; esac; \
	    r=$$((r+1)); \
	  done; \
	  caught=$$((wrong+hang+other)); \
	  verdict="caught $$caught/$(PART2_MUT_RUNS) (wrong $$wrong, hang $$hang)"; \
	  [ $$other -gt 0 ] && verdict="$$verdict, crash $$other"; \
	  [ $$caught -eq 0 ] && verdict="SURVIVED 0/$(PART2_MUT_RUNS)"; \
	  return 0; \
	}; \
	undetected=0; \
	for m in $(PART2_MUTANTS); do \
	  name=`$(BUILD)/barrier_mut$$m name`; \
	  stress $(BUILD)/barrier_mut$$m;        plain="$$verdict"; plain_caught=$$caught; \
	  stress $(BUILD)/barrier_mut$${m}_fuzz; fuzz="$$verdict";  fuzz_caught=$$caught; \
	  log=$(BUILD)/barrier_mut$${m}_tsan.log; \
	  TSAN_OPTIONS="abort_on_error=0 $$TSAN_OPTIONS" $(BUILD)/barrier_mut$${m}_tsan small stall_s=5 cap_s=0.5 > $$log 2>&1; \
	  tsan_caught=0; \
	  if grep -q "ThreadSanitizer: data race" $$log; then tsan="DATA RACE reported"; tsan_caught=1; else tsan="no report"; fi; \
	  if grep -q "^FAIL.*HANG" $$log; then tsan="$$tsan; checks: hang"; tsan_caught=1; \
	  elif grep -q "^FAIL" $$log; then tsan="$$tsan; checks: wrong episode/value"; tsan_caught=1; \
	  else tsan="$$tsan; checks: passed"; fi; \
	  printf '%-23s | %-29s | %-29s | %s\n' "$$name" "$$plain" "$$fuzz" "$$tsan"; \
	  if [ $$((plain_caught+fuzz_caught+tsan_caught)) -eq 0 ]; then undetected=$$((undetected+1)); fi; \
	done; \
	if [ $$undetected -ne 0 ]; then echo "$$undetected mutant(s) were caught by nothing"; exit 1; fi
