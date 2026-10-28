# mk/part3.mk -- Part 3 (concurrent counters): correctness, ThreadSanitizer and mutation targets.
# Included by the top-level Makefile (which defines CXX, CPPFLAGS, CXXFLAGS, BUILD and HDRS).

TEST_TARGETS     += test-part3
TSAN_TARGETS     += tsan-part3
MUTATION_TARGETS += mutation-part3

.PHONY: test-part3 tsan-part3 mutation-part3

# Every counter, normal build, full sizes: one PASS/FAIL line per counter, exit code 1 on failure
# (3 if the watchdog had to end a run that made no progress).
test-part3: $(BUILD)/test_counter
	$(BUILD)/test_counter

# The same test under ThreadSanitizer with small sizes. A ThreadSanitizer report makes the program
# exit with a non-zero code, so this target fails on any data race.
tsan-part3: $(BUILD)/test_counter_tsan
	$(BUILD)/test_counter_tsan small

# ---- mutation testing ---------------------------------------------------------------------------
# tests/test_counter.cpp compiled with -DPC_MUTANT=PC_MUTANT_<name> tests ONE deliberately broken
# counter (they are defined in that file). Every mutant is built twice: normally and with
# ThreadSanitizer.
PART3_MUTANTS := plain_long load_store cas_no_retry lock_read_outside block_reserve
# How often the stress test is repeated for each mutant (a race may need several attempts).
PART3_MUT_RUNS ?= 3

$(BUILD)/counter_mut_%: tests/test_counter.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -DPC_MUTANT=PC_MUTANT_$* -o $@ $<

$(BUILD)/counter_muttsan_%: tests/test_counter.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -O1 -g -fsanitize=thread -DPC_MUTANT=PC_MUTANT_$* -o $@ $<

# One table row per mutant.
#   value checks:     in how many of the runs of the normal build the test failed, and which checks
#                     fired in the first run (the CHECKS_FIRED line of the test): final, duplicate,
#                     missing, order, real-time.
#   ThreadSanitizer:  "data race" if the log of the TSan build contains a data-race report.
#                     A failure of the value checks inside the TSan build does not count as TSan.
# The logs are kept in $(BUILD)/counter_mut*.log. The target fails if a mutant is caught by nothing.
mutation-part3: $(foreach m,$(PART3_MUTANTS),$(BUILD)/counter_mut_$(m) $(BUILD)/counter_muttsan_$(m))
	@echo "# Part 3 mutation tests: deliberately broken counters (defined in tests/test_counter.cpp)"
	@printf '%-18s | %-12s | %-34s | %-15s | %s\n' "mutant" "value checks" "checks that fired" "ThreadSanitizer" "verdict"
	@survivors=0; \
	for m in $(PART3_MUTANTS); do \
	    caught=0; r=1; \
	    while [ $$r -le $(PART3_MUT_RUNS) ]; do \
	        $(BUILD)/counter_mut_$$m > $(BUILD)/counter_mut_$$m.run$$r.log 2>&1 || caught=$$((caught + 1)); \
	        r=$$((r + 1)); \
	    done; \
	    fired=`sed -n 's/^CHECKS_FIRED: *//p' $(BUILD)/counter_mut_$$m.run1.log`; \
	    TSAN_OPTIONS="abort_on_error=0" $(BUILD)/counter_muttsan_$$m small > $(BUILD)/counter_muttsan_$$m.log 2>&1; \
	    if grep -q "ThreadSanitizer: data race" $(BUILD)/counter_muttsan_$$m.log; then tsan="data race"; tsan_hit=1; \
	    else tsan="no report"; tsan_hit=0; fi; \
	    if [ $$caught -gt 0 ] && [ $$tsan_hit -eq 1 ]; then verdict="killed by both"; \
	    elif [ $$caught -gt 0 ]; then verdict="killed by the value checks only"; \
	    elif [ $$tsan_hit -eq 1 ]; then verdict="killed by TSan only"; \
	    else verdict="SURVIVED"; survivors=$$((survivors + 1)); fi; \
	    printf '%-18s | %-12s | %-34s | %-15s | %s\n' "$$m" "$$caught/$(PART3_MUT_RUNS) runs" "$$fired" "$$tsan" "$$verdict"; \
	done; \
	echo "# logs: $(BUILD)/counter_mut_*.log $(BUILD)/counter_muttsan_*.log"; \
	if [ $$survivors -ne 0 ]; then echo "# $$survivors mutant(s) survived every test"; exit 1; fi
