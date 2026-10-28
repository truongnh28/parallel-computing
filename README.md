# Parallel Computing assignments

C++20 assignments for the Parallel Computing course (master's programme, Ho Chi Minh City University of Technology, HCMUT). Each assignment lives in its own folder with its own Makefile, tests, benchmarks, raw results and report.

| Folder | Topic |
|---|---|
| [`Assignment-1/`](Assignment-1/) | Mutex locks (TAS, TTAS, MCS), a sense-reversing barrier and concurrent counters, with correctness tests and a performance study. Statement: [`Assignment1_18092026.pdf`](Assignment-1/Assignment1_18092026.pdf) |
| [`Assignment-2/`](Assignment-2/) | Statement only, not started: [`Assignment2_v1_23102026.pdf`](Assignment-2/Assignment2_v1_23102026.pdf) |
| [`cmake/`](cmake/) | Build rules shared by every assignment (`PcAssignment.cmake`) |
| [`CMakeLists.txt`](CMakeLists.txt) | One CMake project that contains every assignment folder |

Each assignment's README explains how to build it, test it, run its measurements and rebuild its report.

## Requirements

- A C++20 compiler: Apple clang (macOS, arm64), or g++ / clang++ (Linux, x86-64 or arm64).
- `make`, or CMake 3.20 or newer.
- Optional: OpenMP. The OpenMP baselines are compiled only when the compiler supports it. On macOS, install it with `brew install libomp`.

No other library is needed to build and test the code. Each assignment's README lists the extra tools for measurements and for the report.

## Two ways to build

Make is the reference build. It drives the whole workflow of an assignment: tests, mutation testing, the measurement campaign, figures, the report and the submission zip.

```sh
cd Assignment-1
make && make test
```

CMake builds the same programs with the same flags. Use it for an IDE (CLion, VS Code) and for `ctest`. It covers building and testing only.

```sh
cmake -S . -B build                 # configure every assignment
cmake --build build -j              # build every program
ctest --test-dir build              # run every correctness test
ctest --test-dir build -R Assignment-1/test_locks   # run one test
```

A single assignment can also be configured on its own: `cmake -S Assignment-1 -B cmake-build-a1`.

### CMake targets and options

Every `tests/*.cpp` and `bench/*.cpp` file is a standalone program. CMake target names start with the folder name, so two assignments can both have a `test_locks`. The executable keeps the plain name.

| Source | CMake target | Executable | ctest name |
|---|---|---|---|
| `Assignment-1/tests/test_locks.cpp` | `Assignment-1_test_locks` | `build/Assignment-1/test_locks` | `Assignment-1/test_locks` |
| same, ThreadSanitizer build | `Assignment-1_test_locks_tsan` | `build/Assignment-1/test_locks_tsan` | `Assignment-1/test_locks_tsan` |
| `Assignment-1/bench/bench_lock.cpp` | `Assignment-1_bench_lock` | `build/Assignment-1/bench_lock` | none |
| every header in `include/` | `Assignment-1_header_check` | none | none |

`<folder>_header_check` compiles each header on its own, which checks that the header includes everything it uses. It also gives IDEs the compile flags for the headers. It is not part of the default build: `cmake --build build --target Assignment-1_header_check`.

| Option | Default | Effect |
|---|---|---|
| `CMAKE_BUILD_TYPE` | `Release` (`-O2`) | Use `Debug` for debugging; measure only with `Release` |
| `PC_TSAN` | `ON` | Also build a ThreadSanitizer variant `<test>_tsan` of every test, run with the argument `small` |
| `PC_OPENMP` | `ON` | Compile the OpenMP baselines when the compiler supports OpenMP |

Example: `cmake -S . -B build -DPC_TSAN=OFF`.

## Adding an assignment

1. Create a folder with `include/`, `tests/` and `bench/`.
2. Add a `CMakeLists.txt` to it:

   ```cmake
   cmake_minimum_required(VERSION 3.20)
   project(Assignment-2 LANGUAGES CXX)

   include(${CMAKE_CURRENT_LIST_DIR}/../cmake/PcAssignment.cmake)
   pc_add_assignment()
   ```

3. Configure again. The top-level `CMakeLists.txt` picks up every folder that has a `CMakeLists.txt`.

A program that needs extra macros or should stay out of the default build is added with `pc_add_program`. `Assignment-1` adds two such programs: the barrier test with random delays injected, and the lock benchmark with a different spin-wait hint.

```cmake
pc_add_program(test_barrier_fuzz tests/test_barrier.cpp TEST DEFINITIONS PC_BARRIER_FUZZ)
pc_add_program(bench_lock_yield bench/bench_lock.cpp EXCLUDE_FROM_ALL DEFINITIONS PC_RELAX_YIELD)
```

The arguments are `EXCLUDE_FROM_ALL`, `TSAN`, `TEST`, `TEST_ARGS <args...>` and `DEFINITIONS <macros...>`.
