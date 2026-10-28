# mk/part1.mk -- Part 1 (mutex locks): correctness, ThreadSanitizer and mutation targets.
# Included by the top-level Makefile.

TEST_TARGETS     += test-part1
TSAN_TARGETS     += tsan-part1
MUTATION_TARGETS += mutation-part1

# All locks, normal build, full sizes.
test-part1: $(BUILD)/test_locks
	$(BUILD)/test_locks

# All locks under ThreadSanitizer, smaller sizes (TSan slows the program down 5-15x).
tsan-part1: $(BUILD)/test_locks_tsan
	$(BUILD)/test_locks_tsan small

# ---- mutation testing ---------------------------------------------------------------------------
# Each entry is MACRO:lock. Compiling with -DPC_MUTANT_<MACRO> enables one deliberately broken
# variant inside include/lock.h; the test is then run only on the lock that was broken.
PART1_MUTANTS := TAS_CHECK_THEN_SET:tas \
                 TAS_RELEASE_RELAXED:tas \
                 MCS_NO_WAIT_SUCCESSOR:mcs \
                 MCS_ALWAYS_CLEAR_TAIL:mcs
PART1_MUTANT_NAMES := $(foreach e,$(PART1_MUTANTS),$(firstword $(subst :, ,$(e))))

$(BUILD)/locks_mut_%: tests/test_locks.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -DPC_MUTANT_$* -o $@ $<

$(BUILD)/locks_muttsan_%: tests/test_locks.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -O1 -g -fsanitize=thread -DPC_MUTANT_$* -o $@ $<

# For every mutant: run the stress test three times (normal build) and once under ThreadSanitizer.
# The exit status of test_locks is 0 = passed (mutant MISSED), 1 = mutual exclusion check failed,
# 2 = watchdog (deadlock). "TSan caught" means that TSan printed a data race report; a failure of
# the test's own checks inside the TSan build is shown separately, because that is the stress test
# catching the bug, not TSan. The target fails only if a mutant is caught by nothing.
mutation-part1: $(foreach n,$(PART1_MUTANT_NAMES),$(BUILD)/locks_mut_$(n) $(BUILD)/locks_muttsan_$(n))
	@echo "# Part 1 mutation tests (deliberately broken locks, see the MUTANT blocks in include/lock.h)"
	@printf '%-22s %-5s %-36s %-44s %s\n' "mutant" "lock" "stress test, 3 runs" "ThreadSanitizer build, 1 run" "verdict"
	@survivors=0; \
	for entry in $(PART1_MUTANTS); do \
	    m=$${entry%%:*}; l=$${entry##*:}; \
	    caught=0; why=""; \
	    for run in 1 2 3; do \
	        $(BUILD)/locks_mut_$$m only=$$l watchdog=5 > $(BUILD)/locks_mut_$$m.run$$run.log 2>&1; rc=$$?; \
	        case $$rc in \
	            0) ;; \
	            1) caught=$$((caught + 1)); why="mutual exclusion";; \
	            2) caught=$$((caught + 1)); why="deadlock";; \
	            *) why="ERROR, exit status $$rc";; \
	        esac; \
	    done; \
	    if [ $$caught -gt 0 ]; then stress="CAUGHT $$caught/3 ($$why)"; else stress="MISSED 0/3 $$why"; fi; \
	    TSAN_OPTIONS="halt_on_error=1 abort_on_error=0" $(BUILD)/locks_muttsan_$$m small only=$$l watchdog=10 > $(BUILD)/locks_muttsan_$$m.log 2>&1; rc=$$?; \
	    tsan_hit=0; \
	    if grep -q "WARNING: ThreadSanitizer: data race" $(BUILD)/locks_muttsan_$$m.log; then tsan="CAUGHT (data race report)"; tsan_hit=1; \
	    elif [ $$rc -eq 1 ]; then tsan="no report (test check: mutual exclusion)"; \
	    elif [ $$rc -eq 2 ]; then tsan="no report (test watchdog: deadlock)"; \
	    elif [ $$rc -eq 0 ]; then tsan="MISSED (no report, test passed)"; \
	    else tsan="ERROR, exit status $$rc"; fi; \
	    if [ $$caught -gt 0 ] && [ $$tsan_hit -eq 1 ]; then verdict="killed by both"; \
	    elif [ $$caught -gt 0 ]; then verdict="killed by stress test"; \
	    elif [ $$tsan_hit -eq 1 ]; then verdict="killed by TSan only"; \
	    else verdict="SURVIVED"; survivors=$$((survivors + 1)); fi; \
	    printf '%-22s %-5s %-36s %-44s %s\n' "$$m" "$$l" "$$stress" "$$tsan" "$$verdict"; \
	done; \
	echo "# logs: $(BUILD)/locks_mut_*.log $(BUILD)/locks_muttsan_*.log"; \
	if [ $$survivors -ne 0 ]; then echo "# $$survivors mutant(s) survived every test"; exit 1; fi

# ---- cpu_relax comparison -----------------------------------------------------------------------
# The lock benchmark built with the AArch64 YIELD hint instead of ISB (see platform.hpp and
# docs/part1.md). Not part of `make all`; build it explicitly: make build/bench_lock_yield
$(BUILD)/bench_lock_yield: bench/bench_lock.cpp $(HDRS) | $(BUILD)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -DPC_RELAX_YIELD -o $@ $<

.PHONY: test-part1 tsan-part1 mutation-part1
