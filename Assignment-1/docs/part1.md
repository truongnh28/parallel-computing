# Part 1 — Mutex locks: implementation notes

Raw material for the report. Everything here was produced on one machine (Apple M5, 4 performance + 6 efficiency cores, macOS 27, Apple clang 21.0, `-std=c++20 -O2`), on 14/10/2026.

> **All timings in this file were taken while other builds and tests were running on the same machine (1-minute load average between 3 and 40 on 10 cores).** They were used only to choose parameters and to check linearity. None of them is a result. The measurement campaign has to be run later, alone.

## 1. Files

| File | Content |
|---|---|
| `include/lock.h` | The locks, the baseline wrappers, the list `PC_FOR_EACH_LOCK`, and four mutants under `#if defined(PC_MUTANT_...)` |
| `tests/test_locks.cpp` | Correctness tests, the same templated code for every lock |
| `bench/bench_lock.cpp` | The assignment's loop; one CSV row per run |
| `bench/sleep_latency.cpp` | Real duration of `sleep_for`, `yield`, `cpu_relax` |
| `mk/part1.mk` | `make test-part1`, `make tsan-part1`, `make mutation-part1`, `make build/bench_lock_yield` |

Lock names printed by `build/bench_lock list` on macOS:

```
tas ttas mcs tas_bo_sleep tas_bo_spin tas_bo_yield ttas_bo_sleep ttas_bo_spin ttas_bo_yield std_mutex pthread_mutex os_unfair_lock
```

On Linux `os_unfair_lock` is replaced by `pthread_spinlock`, and `omp_lock` is added when the compiler accepts `-fopenmp`.

## 2. Specification of each lock

Every lock has exactly `void acquire()` and `void release()`, as in the assignment's skeleton. `release()` must be called by the thread that holds the lock. No lock is reentrant. All names are in namespace `pc`; `lock_tas`, `lock_ttas`, `lock_mcs` are also visible without the namespace.

| Lock | State | `acquire()` | `release()` | Fair |
|---|---|---|---|---|
| `lock_tas` | `atomic<bool> locked_` | repeat `exchange(true)` until it returns `false`; one `cpu_relax()` after each failure | `store(false)` | no |
| `lock_ttas` | same | wait with `load()` until the flag reads `false`, then one `exchange(true)`; if it returns `true`, go back to waiting | `store(false)` | no |
| `lock_tas_bo<B>` | same | one `exchange`; on failure create a `B` and alternate `B.pause()` and `exchange` | `store(false)` | no |
| `lock_ttas_bo<B>` | same | TTAS attempt; after each lost `exchange`, `B.pause()` (Herlihy and Shavit's BackoffLock). While the lock is merely held the thread keeps reading and does not back off | `store(false)` | no |
| `lock_mcs` | `atomic<mcs_node*> tail_`; per thread one `mcs_node {next, locked}` | reset own node; `pred = tail_.exchange(me)`; if `pred` is null the lock is ours; otherwise `pred->next = me` and wait until `me->locked` becomes `false` | if `next` is null, try `tail_` CAS `me → null` and return on success; otherwise wait until `next` is set; then `next->locked = false` | FIFO |

Backoff policies `B` (from `backoff.hpp`): `SleepBackoff` is the code printed in the assignment (`sleep_for(delay)`, delay doubles from 100 ns to 100 000 ns); `SpinBackoff` spins a random number of `cpu_relax()` inside a window that doubles from 4 to 1024; `YieldBackoff` spins 64 times and then calls `std::this_thread::yield()`. In both backoff locks the first attempt is made before the policy object exists, so an uncontended `acquire()` costs the same as without backoff (`SpinBackoff`'s constructor reads a `thread_local` seed).

Baselines, same two functions, calls inlined: `lock_std_mutex` (`std::mutex`), `lock_pthread_mutex` (`pthread_mutex_t`, default attributes), `lock_os_unfair` (macOS `os_unfair_lock`), `lock_pthread_spin` (Linux `pthread_spinlock_t`), `lock_omp` (`omp_lock_t`, only with OpenMP).

## 3. Memory-order reasoning

The rule: **taking the lock is an acquire operation, giving it up is a release operation.** The release store of the previous owner and the acquire read of the next owner form a happens-before edge, and through it everything written inside the critical section is visible to the next owner.

| Operation | Order | Reason |
|---|---|---|
| TAS/TTAS `exchange(true)` | `acquire` | The winning exchange reads the `false` written by the last `release()`. |
| TAS/TTAS `store(false)` | `release` | Publishes the writes of the critical section. |
| TTAS waiting `load()` | `relaxed` | Only a hint for when to try. Ownership is decided by the exchange that follows; a stale value costs one more loop. |
| MCS `me->next = null`, `me->locked = true` | `relaxed` | The node is not reachable by any other thread yet. It becomes reachable through `tail_.exchange` and `pred->next.store`, both release-ordered and executed later by the same thread, which publishes these two stores. |
| MCS `tail_.exchange(me)` | `acq_rel` | Release half publishes the node. Acquire half is the lock acquisition when the lock was free: it pairs with the releasing CAS of the previous owner. |
| MCS `pred->next.store(me)` | `release` | The predecessor reads it with `acquire` before it writes `me->locked`. |
| MCS waiting `me->locked.load()` | `acquire` | This read is the lock acquisition in the contended case; pairs with the predecessor's release store. |
| MCS `me->next.load()` in `release()` | `acquire` | The successor's node must be initialised before we write its `locked` flag. |
| MCS `tail_` CAS, success | `release` | Frees the lock; pairs with the next thread's `exchange`. |
| MCS `tail_` CAS, failure | `relaxed` | On failure the loaded value is not used; we only learn that another thread has appended itself. |
| MCS `succ->locked.store(false)` | `release` | Hands the lock over. |

There is no `seq_cst` operation in the file. On AArch64 the generated code is `swpab` (exchange, acquire) and `stlrb` (store, release); a relaxed store would be a plain `strb`.

## 4. Memory layout

- The lock word of every lock is declared `alignas(pc::kCacheLine)`. The alignment of a member raises the alignment and the size of the class, so `sizeof(lock)` is a whole number of cache lines and nothing else can share the line.
- `pc::kCacheLine` is 128 on Apple arm64 and 64 elsewhere. Measured on this machine: `sizeof` and `alignof` are 128 for all twelve lock types and for `mcs_node`.
- `mcs_node` is itself `alignas(kCacheLine)`: the `locked` flag a thread spins on is on a line that only its predecessor writes, once.
- The wrappers align the library lock in the same way, so the comparison is not distorted by false sharing in a baseline.

## 5. Known limitations

1. **`lock_mcs`: one MCS lock per thread at a time.** `acquire()` has no parameter, so the queue node is `inline static thread_local mcs_node node_`, shared by all `lock_mcs` objects. A thread that holds one `lock_mcs` and acquires a second one would reuse the node that is still linked in the first queue. Nested MCS locks are therefore not supported. Sequential use of different `lock_mcs` objects is fine. After `release()` returns no other thread holds a pointer to the node, so thread exit is safe.
2. **All spinlocks assume that a waiting thread has a core.** With more runnable threads than cores they slow down; MCS is the worst case because the lock can be handed to a thread that is not running (section 7.3 has a number).
3. No `try_lock`, no timeout, not reentrant, no owner check: `release()` by a non-owner is undefined behaviour.
4. `SleepBackoff` sleeps far longer than requested (section 9), so the sleep-backoff locks behave like "one thread runs in a burst, the others sleep".
5. On macOS the address of the `thread_local` node is obtained by a call through the TLS descriptor. In the benchmark loop the compiler hoists this call out of the loop (checked with `-S`), so it is not in the measured cost; where `acquire()` is not inlined into a loop it is paid on every call.

## 6. Correctness strategy

`tests/test_locks.cpp` runs the same code for every lock in `PC_FOR_EACH_LOCK`, including the baselines.

| # | Property | How it is checked |
|---|---|---|
| 1 | Mutual exclusion, fixed work | Threads run `acquire; critical section; release` for a fixed number of iterations: 1 × 200 000, 2 × 100 000, `hw` × 20 000. The critical section increments a **non-atomic** `long long` (read, add one, write) and an occupancy counter `inside`. Pass if the final counter equals `threads × iters` and no thread ever found `inside != 0` on entry. |
| 2 | Mutual exclusion, fixed time | Same critical section for 300 ms with 4, `hw` and `2 × hw` threads; each thread counts its acquisitions. Pass if the counter equals the sum of the per-thread counts and no thread found `inside != 0`. |
| 3 | Oversubscription | The `2 × hw` run of test 2 (20 threads on 10 hardware threads). It is time-bounded, so a collapsing spinlock cannot make the test run long. |
| 4 | No deadlock | A watchdog thread; if a sub-test is not finished after 20 s it prints `FAIL <lock> <sub-test>: DEADLOCK` and ends the process with status 2. |
| 5 | Fairness (measured, not judged) | max/min of the per-thread acquisition counts of test 2. |
| 6 | Data races | The same program under ThreadSanitizer with `small` (10× fewer iterations, 100 ms). |
| 7 | The tests themselves | Mutation testing, section 8. |

Exit status: 0 pass, 1 mutual exclusion failed, 2 watchdog. Arguments: `small`, `only=<lock>`, `skip=<lock>`, `watchdog=<s>`, `ms=<ms>`.

One detail matters for TSan: the test's own atomics (`inside`, `violations`, the `stop` flag) use `memory_order_relaxed`. With a stronger order they would themselves create happens-before edges between the threads, and TSan would no longer see that a broken lock fails to create one. The `TAS_RELEASE_RELAXED` mutant below is detected only because of this.

## 7. Results

### 7.1 Stress tests

`make test-part1`: **12 of 12 locks PASS** on macOS arm64 (12 s). In an emulated x86-64 Linux container (Debian 12, g++ 12.2, `-fopenmp`, 6 CPUs): **13 of 13 PASS**, including `pthread_spinlock` and `omp_lock`.

### 7.2 ThreadSanitizer

`make tsan-part1`: **12 of 12 PASS, no report**, 8–16 s. The three macOS baselines (`std::mutex`, `pthread_mutex_t`, `os_unfair_lock`) are intercepted by TSan and gave no false positive. Not run: TSan on Linux (TSan does not start in the emulated container). `omp_lock` under TSan is therefore untested; an OpenMP runtime that is not instrumented may produce false reports. If it does, run `build/test_locks_tsan small skip=omp_lock` and say so; the skip is printed.

### 7.3 Fairness and oversubscription

max/min of per-thread acquisitions in 300 ms, two runs of `make test-part1` (run A at load ≈ 3, run B at load ≈ 16). 1.00 means every thread got the same number.

| Lock | 4 threads A / B | 10 threads A / B | 20 threads A / B |
|---|---|---|---|
| `tas` | 1.61 / 1.03 | 1.45 / 4.17 | 6.91 / 2.68 |
| `ttas` | 2.35 / 1.49 | 1.29 / 1.38 | 1.56 / 10.57 |
| `mcs` | **1.00 / 1.00** | **1.00 / 1.11** | 27.00 / 17.33 |
| `tas_bo_sleep` | 1.15 / 1.16 | 1.32 / 1.33 | 1.21 / 1.64 |
| `tas_bo_spin` | 1.15 / 1.08 | 1.34 / 1.44 | 1.92 / 3.41 |
| `tas_bo_yield` | 1.52 / 1.37 | 6.03 / 1.63 | 2.13 / 3.30 |
| `ttas_bo_sleep` | 1.18 / 1.09 | 1.70 / 1.38 | 1.87 / 1.63 |
| `ttas_bo_spin` | 2.02 / 1.20 | 1.47 / 1.59 | 2.53 / 4.49 |
| `ttas_bo_yield` | 3.78 / 1.14 | 1.55 / 1.65 | 3.52 / 3.85 |
| `std_mutex` | 1.03 / 1.13 | 1.06 / 2.10 | 1.40 / 5.46 |
| `pthread_mutex` | 1.03 / 1.20 | 1.30 / 1.06 | 1.65 / 3.15 |
| `os_unfair_lock` | 1.10 / 9.72 | 1.65 / 5.58 | 1.86 / 9.28 |

What can be said from these numbers, and what cannot:

- `mcs` is the only lock with a ratio of 1.00 at 4 threads in both runs, and it stays near 1 at 10 threads. This is the FIFO property.
- The other ratios change by a factor of 2–9 between the two runs. They are not stable enough to rank the unfair locks; the campaign needs repetitions.
- FIFO order does not imply equal counts when threads are descheduled. A thread that is preempted *between* `release()` and its next `acquire()` is not in the queue and loses whole rounds. That is why `mcs` shows 17–27 at 20 threads, and a 50 ms run at load 40 gave 38 at 10 threads.
- **MCS collapses under oversubscription.** Total acquisitions in 300 ms at 4 / 10 / 20 threads: `mcs` 3 141 028 / 1 041 040 / **418** (run A) and 1 965 131 / 53 793 / **394** (run B); `tas` 4 871 496 / 1 498 608 / 667 141; `ttas` 1 801 573 / 968 478 / 804 477; `std_mutex` 21 383 534 / 16 629 850 / 16 019 470. At 20 threads MCS completes about 1 400 hand-overs per second: the lock is handed to a thread that is not on a core, and everybody waits until the scheduler runs it.
- Run B, 10 threads, load 16: `mcs` dropped to 53 793 while `tas` did not drop. Any background load makes the machine oversubscribed for MCS.
- The sleep-backoff locks show the highest counts (48–58 million): the waiters are asleep, and one thread runs alone at uncontended speed. With an empty critical section this measures bursts, not hand-overs.

## 8. Mutation testing

`make mutation-part1` compiles `tests/test_locks.cpp` with one `-DPC_MUTANT_<NAME>` at a time (normal build and TSan build) and runs it on the lock that was broken: three times the stress test (`watchdog=5`), once under TSan (`small`, `halt_on_error=1`). "TSan caught" means a data-race report; a failure of the test's own checks in the TSan build is listed separately. The target fails only if a mutant survives everything.

| Mutant | Change (one place in `lock.h`) | Stress test (3 runs) | TSan build (1 run) | Verdict |
|---|---|---|---|---|
| `TAS_CHECK_THEN_SET` | `acquire()`: `while (load()) relax; store(true);` instead of `exchange` | caught 3/3, mutual exclusion | data race reported | killed by both |
| `TAS_RELEASE_RELAXED` | `release()`: `store(false, relaxed)` | **missed 0/3** | data race reported | **killed by TSan only** |
| `MCS_NO_WAIT_SUCCESSOR` | `release()` returns after the failed CAS instead of waiting for `next` | caught 3/3, deadlock | no race report; the watchdog fires | killed by stress test |
| `MCS_ALWAYS_CLEAR_TAIL` | `release()` stores `tail_ = nullptr` before handing over | caught 3/3, deadlock | data race reported | killed by both |

The result was the same in three complete runs. Notes:

- `TAS_CHECK_THEN_SET`, sample output: `tas fixed-iters threads=10 iters=20000: counter=151110 expected=200000 (lost 48890), threads seen inside together=193122`.
- `TAS_RELEASE_RELAXED` is the limit of stress testing. On AArch64 the mutant turns `stlrb` into `strb`; the architecture then allows the unlock to become visible before the last write of the critical section, but this was never observed in about 6 million critical sections per run. On x86-64 the mutant compiles to the same instruction as the correct lock, so no stress test can ever catch it there. TSan catches it because it tracks the happens-before relation defined by the memory orders, not the behaviour of the processor.
- `MCS_NO_WAIT_SUCCESSOR` is a liveness bug without a data race, so TSan has nothing to report; only the watchdog sees it.
- `MCS_ALWAYS_CLEAR_TAIL` breaks mutual exclusion and also strands a waiter. The watchdog fires before the sub-test can print its counter, so the stress column says deadlock.

## 9. Benchmark program

`bench_lock lock=<name> threads=<p> iters=<I> outside_work=<OW> n=<prime>` runs the assignment's loop and prints one CSV row with `part=lock`, `ops = p × I`, `ok`, and `extra = thr_min_s:..;thr_max_s:..;relax:isb|yield|pause` (the loop time of the fastest and of the slowest thread: all threads do the same number of acquisitions, so unfairness shows up as time). `n` is rejected if it is not prime.

### 9.1 The critical section is really executed

Assembly of the TAS instantiation (`c++ -O2 -S`, Apple clang 21), outer loop body:

```asm
        swpab   w27, w9, [x8]        ; exchange(true, acquire)
        tbz     w9, #0, LBB52_5
LBB52_4: isb                          ; cpu_relax
        swpab   w27, w9, [x8]
        tbnz    w9, #0, LBB52_4
LBB52_5: mov     x0, x25              ; n, after opaque()
        bl      __ZN2pc8is_primeEi   ; critical section
        add     x22, x22, w0, sxtw   ; sum += result
        stlrb   wzr, [x8]            ; store(false, release)
        ...
LBB52_6: mov     x0, x25
        bl      __ZN2pc8is_primeEi   ; outside work, once per j
        add     x22, x22, w0, sxtw
        subs    x28, x28, #1
        b.ne    LBB52_6
```

Linearity, one thread, `lock=tas`, minimum of 5 runs:

| N | iters | seconds | ns per call | ns per loop iteration of `is_prime` |
|---|---|---|---|---|
| 11 | 2 222 222 | 0.00949 | 4.3 | 0.474 |
| 101 | 202 020 | 0.00935 | 46.3 | 0.468 |
| 1009 | 19 861 | 0.00927 | 466.8 | 0.464 |
| 10007 | 1 999 | 0.00932 | 4 661.9 | 0.466 |
| 100003 | 200 | 0.00931 | 46 533 | 0.465 |

| OUTSIDE_WORK (N = 1009, iters = 20 000) | seconds | seconds / (1 + OW) |
|---|---|---|
| 0 | 0.00938 | 0.00938 |
| 1 | 0.01930 | 0.00965 |
| 3 | 0.03911 | 0.00978 |
| 7 | 0.07508 | 0.00939 |
| 15 | 0.15410 | 0.00963 |

Time is proportional to N − 2 (constant 0.465 ns per iteration over four orders of magnitude) and to 1 + OUTSIDE_WORK (within 4 %). The `ok` column was 1 in every run.

### 9.2 A trap confirmed with GCC

`workload.hpp` says that `noinline` plus a run-time N is enough. With g++ 12.2 `-O2` it is **not**: when `pc::opaque(n)` is applied once before the loop, GCC proves `is_prime` free of side effects and replaces the whole OUTSIDE_WORK loop by one multiplication (`imulq`), so the outside work disappears. Apple clang 21 does not do this. `bench_lock.cpp` (and `bench_counter.cpp`) call `pc::is_prime(pc::opaque(n))` at **every** call, which keeps both calls with both compilers (checked with `-S` on both). Any new benchmark must do the same.

### 9.3 Uncontended cost

One thread, `n=2` (`is_prime` returns at once), 5 000 000 iterations, minimum of 5 runs, nanoseconds per iteration including the call:

| tas | ttas | mcs | tas_bo_* | ttas_bo_* | std_mutex | pthread_mutex | os_unfair_lock |
|---|---|---|---|---|---|---|---|
| 4.2 | 3.6 | 3.7 | 3.5–3.6 | 3.5–3.6 | 4.2 | 3.5 | 2.8 |

All locks are within 1.5 ns of each other; the differences are below the noise of this session.

## 10. Real duration of `sleep_for`, `yield`, `cpu_relax`

`build/sleep_latency`, macOS arm64, single thread, load ≈ 22 (nanoseconds):

| Call | min | median | p99 |
|---|---|---|---|
| `sleep_for(1 ns)` | 4 375 | 13 291 | 18 875 |
| `sleep_for(10 ns)` | 4 375 | 13 291 | 18 333 |
| `sleep_for(100 ns)` | 4 375 | 5 333 | 21 708 |
| `sleep_for(1 µs)` | 3 250 | 4 125 | 12 125 |
| `sleep_for(10 µs)` | 15 167 | 16 000 | 22 292 |
| `sleep_for(100 µs)` | 113 417 | 130 750 | 196 041 |
| `sleep_for(1 ms)` | 1 035 208 | 1 260 416 | 2 050 583 |
| `std::this_thread::yield()` | 125 | 208 | 250 |
| `pc::cpu_relax()` = `isb` | 8.0 | 8.0 | 9.8 |
| `yield` instruction | 0.2 | 0.2 | 0.3 |
| clock read (resolution) | 0 | 0 | 42 |

A second run at load ≈ 40 gave the same minima (2 125–2 458 ns up to 1 µs requested, 13 750 ns for 10 µs) and p99 values 3–20 times larger.

Consequences for the assignment's sample backoff:

- A request of 1 ns to 1 µs costs **at least 2–4 µs** and typically 4–13 µs. The first four doublings of the delay (100, 200, 400, 800 ns) change nothing: the floor is the cost of the system call and the wake-up.
- From 10 µs on the real time is the request plus 4–30 %.
- One failed attempt therefore costs about 1 000 uncontended critical sections of N = 11 (4.3 ns each) and about 10 critical sections of N = 1009 (0.47 µs).
- In the Linux container (virtual machine, emulated x86-64) the median was 1 ms for every request up to 100 µs. That is a property of that virtual machine, not of Linux; it has to be measured again on a real Linux machine.

## 11. `cpu_relax`: ISB or YIELD on Apple Silicon

`platform.hpp` uses `isb` on AArch64; `-DPC_RELAX_YIELD` selects `yield`. Measured cost on the M5: `isb` 8.0 ns, `yield` 0.2 ns. `yield` is in effect a no-op; `isb` is a real pause of a few tens of cycles.

Effect on contended locks, `build/bench_lock` (ISB) against `build/bench_lock_yield`, 7 alternating runs each, seconds as min / median / max, load 17–27:

| Case | ISB | YIELD |
|---|---|---|
| `tas`, 4 thr, N=11, OW=0, 400 000 iters | 0.046 / 0.066 / 0.107 | 0.038 / 0.075 / 0.167 |
| `ttas`, same | 0.062 / 0.070 / 0.167 | 0.063 / 0.076 / 0.107 |
| `mcs`, same | 0.156 / 0.442 / 1.969 | 0.189 / 0.711 / 2.602 |
| `tas`, 4 thr, N=1009, OW=1, 20 000 iters | 0.046 / 0.057 / 0.110 | 0.049 / 0.058 / 0.082 |
| `ttas`, same | 0.043 / 0.048 / 0.050 | 0.045 / 0.049 / 0.058 |
| `mcs`, same | 0.045 / 0.047 / 0.053 | 0.046 / 0.047 / 0.053 |
| `tas`, 8 thr, N=1009, OW=1, 20 000 iters | 0.287 / 0.305 / 0.359 | 0.287 / 0.313 / 0.465 |
| `ttas`, same | 0.106 / 0.112 / 0.165 | 0.110 / 0.122 / 0.149 |
| `mcs`, same | 0.274 / 0.372 / 11.110 | 0.262 / 0.333 / 2.040 |

**No difference can be claimed from this data**: the ranges overlap in every case, and the machine was loaded. The medians favour ISB in 7 of 9 cases by 1–60 %, which is a hint, not a result. Decision for now: keep `isb` (the default of `platform.hpp`), and add the YIELD build as one extra sweep of the campaign (`tas`, `ttas`, `mcs`; the `relax:` field of `extra` tells the rows apart).

## 12. Proposed parameter grid

Measured unit costs on a performance core: `is_prime(11)` with lock ≈ 4.3 ns, `is_prime(1009)` ≈ 0.47 µs, `is_prime(10007)` ≈ 4.66 µs. A contended hand-over with N = 11 cost 29 ns (`tas`), 39 ns (`ttas`), 97 ns (`mcs`) per critical section at 4 threads. Efficiency cores are slower; not measured separately.

| Variable | Values |
|---|---|
| lock | all names of `bench_lock list` |
| threads `p` | 1, 2, 3, 4, 6, 8, 10, 12, 16, 20 (4 = performance cores, 10 = all cores, above 10 = oversubscribed) |
| `outside_work` | 0, 1, 3, 7, 15 |
| `n` | 11, 1009, 10007 |
| repetitions | ≥ 7, implementations interleaved inside each repetition |

**Iterations.** The model of PLAN §1.3 gives the lower bound `T ≥ I · t_N · max(p, 1 + OW)`. One fixed `I` per N cannot keep all runs inside 0.1–0.5 s, because `max(p, 1 + OW)` spans 1 to 20. Proposal: `I = round(W_N / max(p, 1 + OW))`, which puts the lower bound at about 0.19 s for every configuration:

| N | `W_N` | `I` at max = 1 | `I` at max = 4 | `I` at max = 10 | `I` at max = 20 |
|---|---|---|---|---|---|
| 11 | 4 000 000 (assumes 50 ns per contended hand-over) | 4 000 000 | 1 000 000 | 400 000 | 200 000 |
| 1009 | 400 000 | 400 000 | 100 000 | 40 000 | 20 000 |
| 10007 | 40 000 | 40 000 | 10 000 | 4 000 | 2 000 |

Expected run time for `p ≤ 10`: 1–4 times the bound, i.e. 0.2–0.8 s (sanity runs: 1.2× at 4 threads; 1.4× for `ttas` and 3.7–3.8× for `tas`/`mcs` at 8 threads on the loaded machine). Exception: N = 11 with one thread has no hand-over, so it runs only about 20 ms; use `8 × I` there. Because `I` varies, compare configurations by time per iteration or by `ops_per_s`, both derivable from the CSV row. If the runner prefers one `I` per (N, OW) for all thread counts, the same `W_N` with `max(…)` replaced by `1 + OW` gives 0.19 s at one thread and up to 20× more at 20 threads.

**Size.** Full grid: 12 locks × 10 thread counts × 5 OW × 3 N = 1 800 configurations × 7 repetitions ≈ 1 h at 0.3 s per run. Reduced grid: the full OW sweep only for N = 1009, and OW ∈ {0, 3} for N = 11 and 10007: 1 080 configurations ≈ 40 min.

**Timeout for oversubscribed spinlocks (`p` > 10).** `mcs` completed about 1 400 hand-overs per second at 20 threads (section 7.3). With the `I` above (N = 1009, p = 20: 400 000 critical sections) one run would take about 5 minutes. Proposal:

- per-run timeout 30 s; a run that times out is written to the CSV as a timeout (a lower bound on the time), not dropped;
- after 2 timeouts of a configuration, skip its remaining repetitions;
- for `mcs` with `p` > number of cores use `I / 100` (N = 1009, p = 20: 200 iterations per thread, about 3 s), so that the collapse is a measured number and not only a timeout;
- `tas`, `ttas` and the spin-backoff variants completed 2–7× fewer acquisitions at 20 threads than at 4 threads in the timed test; the 30 s timeout is enough for them.

One run of `mcs` at 8 threads took 11.1 s instead of 0.27 s while the machine had a load of 20. The campaign must run alone, and `load1` in each row must be checked.

## 13. Not verified

- **Native Linux.** The code was compiled with g++ 12.2 (`-Wall -Wextra`, no warning, with and without `-fopenmp`) and the tests passed, but in an **emulated x86-64 container** on this Mac. No timing from it is usable. Linux on real hardware, Linux arm64, and newer g++ versions were not tried.
- **TSan on Linux**, and therefore `omp_lock` and `pthread_spinlock` under TSan.
- **OpenMP on macOS**: Apple clang has no `-fopenmp` here, so `lock_omp` is not built on this machine.
- The `TAS_RELEASE_RELAXED` mutant was never seen to fail at run time; its incorrectness is established by the C++ memory model and by TSan, not by observation.
- All performance numbers in this file (see the note at the top).
