# Assignment 1 -- CMake build for the IDE (CLion) and ctest. It builds the same
# programs as the Makefile with the same flags. The Makefile stays the reference
# for the measurement campaign (make bench), mutation testing (make mutation),
# figures and the report.
cmake_minimum_required(VERSION 3.20)
project(Assignment-1 LANGUAGES CXX)

include(${CMAKE_CURRENT_LIST_DIR}/../cmake/PcAssignment.cmake)
pc_add_assignment()

# Part 2: the barrier test with random delays injected at the test points of
# include/barrier.hpp (mk/part2.mk, test-part2).
pc_add_program(test_barrier_fuzz tests/test_barrier.cpp TEST DEFINITIONS PC_BARRIER_FUZZ)

# Part 1: the lock benchmark with the AArch64 YIELD hint in cpu_relax() instead
# of ISB (mk/part1.mk). Not part of the default build, as in the Makefile.
pc_add_program(bench_lock_yield bench/bench_lock.cpp EXCLUDE_FROM_ALL DEFINITIONS PC_RELAX_YIELD)
