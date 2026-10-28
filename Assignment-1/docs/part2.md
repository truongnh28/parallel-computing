# Part 2 — Barrier synchronization: implementation notes

Raw material for the report. Everything here was produced on one machine (Apple M5, 4 performance + 6 efficiency cores, macOS 27.0.1, Apple clang 21.0, `-std=c++20 -O2`), on 14/10/2026. The Linux build was checked in a container on the same machine (Debian, g++ 14.2, aarch64, 6 CPUs).

> **All timings in this file were taken while other builds and tests were running on the same machine (1-minute load average between 7 and 50 on 10 cores).** They were used only to choose test sizes and benchmark parameters. None of them is a result. The measurement campaign has to be run later, alone.

## 1. Files

| File | Content |
|---|---|
| `include/barrier.hpp` | `SenseBarrier<Policy>`, `SenseBarrierBlocking`, and the baselines `StdBarrier`, `CondVarBarrier`, `PthreadBarrier` (Linux), `OmpBarrier` (OpenMP) |
| `tests/test_barrier.cpp` | Correctness test, the same templated code for every barrier; the five mutants (compiled only with `-DPC_MUTANT=k`) |
| `bench/bench_barrier.cpp` | Episodes per second with no work between barriers; one CSV row per run |
| `mk/part2.mk` | `make test-part2`, `make tsan-part2`, `make mutation-part2`, `make tsan-part2-false-positive` |

Names printed by `build/bench_barrier list` on macOS (the names follow Part 1: `tas`, `tas_bo_spin`, …):

```
sr sr_bo_spin sr_bo_yield sr_bo_sleep sr_block std_barrier condvar
```

On Linux `pthread_barrier` is added, and `omp_barrier` when the compiler accepts `-fopenmp`.

| Name | Class | How a thread waits |
|---|---|---|
| `sr` | `SenseBarrier<NoBackoff>` | pure spinning, one `cpu_relax()` per poll |
| `sr_bo_spin` | `SenseBarrier<SpinBackoff>` | randomized exponential backoff in user space (4 … 1024 relax hints) |
| `sr_bo_yield` | `SenseBarrier<YieldBackoff>` | 64 polls, then `std::this_thread::yield()` between polls |
| `sr_bo_sleep` | `SenseBarrier<SleepBackoff>` | the assignment's backoff: `sleep_for(delay)`, `delay` doubles from 100 ns to 100 µs |
| `sr_block` | `SenseBarrierBlocking` | `std::atomic<bool>::wait`, woken by `notify_all` |
| `std_barrier` | `StdBarrier` | `std::barrier<>::arrive_and_wait()` |
| `condvar` | `CondVarBarrier` | `std::mutex` + `std::condition_variable` + generation counter |
| `pthread_barrier` | `PthreadBarrier` | `pthread_barrier_wait` (Linux only; macOS has no `pthread_barrier_t`) |
| `omp_barrier` | `OmpBarrier` | `#pragma omp barrier` inside an OpenMP parallel region |

## 2. Specification

A barrier for `n` threads: **no thread returns from its k-th call of `wait()` before all `n` threads have made their k-th call.** One collective crossing is an *episode*. In addition the barrier must be **reusable** (episode k + 1 works like episode k, without re-initialisation) and must not deadlock when all `n` threads keep calling it.

Interface, identical for every class:

```cpp
Barrier b(n);       // n = fixed number of participants
b.wait(tid);        // thread tid, 0 <= tid < n, once per episode
```

Preconditions: exactly `n` threads take part, each with its own id in `[0, n)`, and every one of them calls `wait` in every episode. The baselines ignore `tid`.

**Why `wait(int tid)`.** The algorithm needs one private bit per thread *and per barrier* (the local sense). Three places could hold it:

| Where | Problem |
|---|---|
| `thread_local` variable inside `wait()` | one bit per thread shared by **all** barrier objects: two barriers used alternately corrupt each other (mutant 5 shows it) |
| a `bool&` owned by the caller | correct, but the caller manages one variable per thread per barrier |
| array inside the barrier, indexed by thread id (**chosen**) | the barrier object is self-contained and a thread can use any number of barriers; the caller must supply distinct ids |

## 3. The algorithm

Shared state: `count_` (arrivals in the current episode) and `sense_` (a flag). Private state: one `local sense` per thread. Initially `sense_ = false`, every local sense is `true`.

```cpp
void wait(int tid) {
    const bool my_sense = local_[tid].sense;
    if (count_.fetch_add(1, acq_rel) == n_ - 1) {     // I am the n-th arriver
        count_.store(0, relaxed);                     // 1. prepare the next episode
        sense_.store(my_sense, release);              // 2. release the others
    } else {
        Policy backoff;
        while (sense_.load(acquire) != my_sense) backoff.pause();
    }
    local_[tid].sense = !my_sense;                    // reverse my sense on the way out
}
```

`fetch_add` returns the value *before* the increment, so the n-th arriver reads `n - 1`.

### 3.1 Why sense reversal makes the barrier reusable

Call `s_k` the value of the local senses in episode k (`s_1 = true`, `s_2 = false`, …). Between two episodes every local sense is `!sense_`.

- **Nobody leaves early.** A thread that is not the last leaves episode k only after it has read `sense_ == s_k`. Only a thread whose `fetch_add` returned `n - 1` writes `s_k`, i.e. after `n` arrivals since the counter was reset. A thread cannot arrive twice in one episode (it cannot leave before the flip), so these are `n` different threads: all have arrived.
- **The condition a thread waits for is stable.** Once `sense_ == s_k`, the next write to `sense_` is the flip of episode k + 1, which needs `n` arrivals in episode k + 1, one of which is the waiting thread's own. So `sense_ == s_k` stays true until that thread has seen it, however long it is delayed. A wake-up cannot be missed.
- **A fast thread cannot run through the next episode.** A thread that comes back for episode k + 1 waits for `s_{k+1} = !s_k`, which is *not* the current value of the flag. This is the difference from a single flag "open/closed": there the flag must be closed again at some moment, and whatever moment is chosen, either a slow thread of episode k has not seen "open" yet (it hangs), or a fast thread of episode k + 1 still sees "open" (it passes early). The sense flag is never reset, it only alternates, and the two episodes that can overlap in time wait for two different values.

### 3.2 Why the counter is reset before the flag is flipped

After the flip, a released thread may immediately arrive for episode k + 1. Its `fetch_add` must find the counter at 0. If the flip came first, that `fetch_add` could read the old value `n`; the late reset would then erase this arrival, episode k + 1 would count at most `n - 1` arrivals, no thread would ever read `n - 1`, and all threads would wait forever. Mutant 2 shows exactly this hang.

## 4. Memory orders

Required property (the barrier acts as a fence between all pairs of threads): *everything any thread wrote before barrier k is visible to every thread after barrier k.*

| Operation | Order | Reason |
|---|---|---|
| `count_.fetch_add(1)` | `acq_rel` | **release**: publishes what this thread wrote before the barrier to the threads that arrive later (a later `fetch_add` reads a value in the release sequence of this one). **acquire**: the last arriver takes over what all earlier arrivers published; it needs this for its own reads after the barrier and it forwards it with the release store on `sense_`. |
| `count_.store(0)` | `relaxed` | It is sequenced before the release store on `sense_`. A thread starts its next episode only after it has read the new `sense_` with acquire (or it is the last arriver itself), so the reset happens-before its next `fetch_add`, which therefore cannot read an older value. |
| `sense_.store(my_sense)` | `release` | Publishes the counter reset and everything the last arriver acquired. |
| `sense_.load()` in the wait loop | `acquire` | Pairs with that store. |
| `local sense` | not atomic | Read and written by its owner only. |

Chain for an arbitrary writer A and reader R: A's write → A's `fetch_add` (release) → last arriver L's `fetch_add` (acquire) → L's `sense_.store` (release) → R's `sense_.load` (acquire) → R's read. If R = L the chain ends at L's `fetch_add`.

**The acquire half of `fetch_add` is observable on this machine.** Mutant 4 changes `acq_rel` to `release`. The expectation from the lecture note was "no visible effect on real hardware, only ThreadSanitizer sees it". On the Apple M5 the plain stress test caught it in 8 of 20 runs: the **last arriver** returned from the barrier and read a stale slot of another thread ("thread 1 passed barrier 85118 EARLY: thread 2 is still in episode 85117"). In an instrumented copy that records who was the last arriver, the reporting thread was the last arriver in all 6 episode-check failures observed (48 runs); the reporter of the rarer handoff-check failures was not classified. Explanation: on AArch64 a `fetch_add` without acquire compiles to `LDADDL` instead of `LDADDAL` (checked in the binaries with `objdump`). `LDADDL` does not order the following loads after it, so the core may execute the last arriver's later reads before the `fetch_add`. A waiting thread is not affected because it goes through the acquire load of `sense_`. The lecture's experiment looked at a waiting thread, which is why it saw nothing.

## 5. Memory layout

```
SenseBarrier object (alignment = cache line)
 line 0:  n_, local_ (vector header: pointer to the slots)      read-only after construction
 line 1:  count_                                                written by every arrival
 line 2:  sense_                                                read by all waiters, written once per episode
heap:     local_[0], local_[1], ...   one cache line each       written by its owner once per episode
```

- `count_` and `sense_` are on separate lines (`alignas(pc::kCacheLine)`: 128 bytes on Apple Silicon, 64 elsewhere). Otherwise every arrival would invalidate the line that the waiting threads poll, and the cost of an episode would grow from O(n) to O(n²) cache misses.
- Each local sense has its own line; a plain `bool[n]` would put all of them on one line and every thread would invalidate it once per episode.
- `sizeof(SenseBarrier<>)` is 384 bytes on Apple Silicon, plus `n` × 128 bytes on the heap.

Expected cost of one episode (to be confirmed by the measurements): `n` `fetch_add`s on one line, served one after the other, then one invalidation of the `sense_` line followed by `n - 1` re-reads. Both are O(n) on the critical path.

## 6. The other variants

**`SenseBarrierBlocking`.** Same protocol and same memory orders. The waiter runs `while (sense_.load(acquire) != my_sense) sense_.wait(!my_sense, acquire);` and the last arriver calls `sense_.notify_all()` after the flip. `wait(old)` re-checks the value atomically with going to sleep, so a flip between the load and the wait is not lost; the loop handles spurious returns. Two facts for the analysis: (1) libc++ polls before it blocks (64 polls, then polling for 4 µs, `__atomic/atomic_sync.h`), so `sr_block` is "spin briefly, then block"; (2) for a 1-byte atomic the libraries wait on an internal table entry selected by the address, not on the variable itself.

**`CondVarBarrier`.** `count_` and `generation_` protected by one mutex. The last arriver resets the counter, increments the generation and calls `notify_all`; the others wait until the generation differs from the one they arrived in. The generation counter does the job of the sense flag (it tells episode k from episode k + 1) and also filters spurious wake-ups.

**`OmpBarrier`.** `#pragma omp barrier` synchronises the threads of the enclosing OpenMP parallel region. In a thread created with `std::thread` it does nothing useful (that thread is a team of one). The class therefore has the common interface, but it must be called from an OpenMP team of exactly `n` threads; the test and the benchmark each have a separate launcher for it (`#pragma omp parallel num_threads(n)`, `omp_set_dynamic(0)`, team size checked). In the benchmark the clock starts inside a `single` construct, once the whole team exists, and stops after the parallel region; the interval contains two extra implicit barriers, negligible against `iters` episodes. The waiting behaviour of the OpenMP barrier depends on `OMP_WAIT_POLICY` / `GOMP_SPINCOUNT`; the report must state the setting used.

## 7. Limitations

- **Fixed number of participants.** There is no `arrive_and_drop`. If one thread stops calling `wait`, all others wait forever.
- **Thread ids.** Two threads using the same id, or an id outside `[0, n)`, break the barrier; this is not checked.
- **No completion function** (the `std::barrier` feature).
- **Spinning variants burn a core per waiting thread**, and they collapse under oversubscription: an episode needs all `n` threads to run, and a waiting thread that spins through its time slice keeps the core away from the thread everybody is waiting for. On the loaded machine `sr` reached 6 × 10⁵ episodes/s with 10 threads and about 40–200 episodes/s with 12–20 threads on 10 cores (four orders of magnitude). `sr_bo_yield`, `sr_bo_sleep` and `sr_block` do not collapse.
- **`sr_bo_sleep`**: a `sleep_for` of 100 ns really lasts tens of microseconds (system call + timer), so one late wake-up costs far more than the episode itself.
- **Centralized**: every thread touches the same two cache lines, cost O(n) per episode. Tree and dissemination barriers (not required here) avoid this.
- **Two kinds of cores** on Apple Silicon and no thread pinning on macOS: with more than 4 threads some participants run on efficiency cores and the slowest one sets the pace.

## 8. Testing the correctness

### 8.1 Checks

| Check | How | Catches |
|---|---|---|
| **Episode check** | In episode `e` each thread writes `e` into its own slot, calls `wait`, then reads all slots: every slot must be `>= e` (otherwise I passed **early**) and `<= e + 1` (otherwise somebody passed barrier `e + 1` without me). | early pass, broken reuse |
| **Handoff check** | Two barriers `a`, `b` used alternately: write own slot → `a.wait` → read all slots → `b.wait`. Nothing is written between `a` and `b`, so every slot must be **exactly** the round number. The slots are plain (non-atomic) variables. | wrong value; under ThreadSanitizer a missing happens-before relation (memory orders); state shared between two barrier objects |
| **Watchdog** | A separate thread aborts with a message and exit code 2 if the sum of all episode numbers has not changed for 10 s (30 s in the ThreadSanitizer build). It prints the episode each thread has reached. | deadlock, lost wake-up |
| **Schedule fuzzing** | `barrier.hpp` has test points (`PC_BARRIER_TEST_POINT()`, empty in normal builds) between the shared-memory steps. The build with `-DPC_BARRIER_FUZZ` puts a random delay at each of them: nothing (≈ 73 %), a spin of 0–255 relax hints (25 %), a `yield` (1.6 %), a sleep of 1–100 µs (0.1 %). All checks stay the same. | faults that need a thread to be delayed between two specific steps |

Phases, run for every barrier (`hw` = hardware threads, 10 here):

| Phase | Threads | Target episodes | Purpose |
|---|---|---|---|
| `single` | 1 | 10 000 | `n = 1`: `wait` must return immediately |
| `reuse` | 4 | 300 000 | very many consecutive episodes without delay (reuse faults appear from episode 2 on) |
| `all-cores` | `hw` | 100 000 | every core busy, mixed P and E cores |
| `jitter` | 4 | 50 000 | random delay before each arrival (same distribution as above): changing arrival order |
| `oversub` | 2 × `hw` | 300 for pure spinning, 5 000–20 000 otherwise, with random delays | preemption in the middle of the protocol |
| `handoff` | 4 | 100 000 rounds (two barrier episodes each) | handoff check |

Targets are multiplied by 1.0 for the spinning variants, 0.3 for `sr_block` / `std_barrier` / `omp_barrier` and 0.1 for `sr_bo_sleep` / `condvar` / `pthread_barrier` (an episode there costs tens of microseconds). Each phase also has a **time budget** (1.5 s; 0.5 s in the fuzzing build): when it is used up, thread 0 sets a common last episode `e + 1` between leaving barrier `e` and arriving at barrier `e + 1`. At that moment no thread can be past barrier `e + 1`, so all threads stop after the same episode. The output states the number of episodes really completed. This keeps the run time bounded on any machine, in particular for oversubscribed spinning.

Exit code: 0 = all pass, 1 = a check failed, 2 = hang. The first failure ends the process (`std::_Exit`), because the other threads may be stuck inside a broken barrier and can never be joined.

### 8.2 Must the slot array be atomic?

- **Episode check: yes.** Thread j reads slot i after barrier `e` while thread i, which also left barrier `e`, may already be writing `e + 1`. Nothing orders these two accesses, even with a perfect barrier, so a plain variable would be a data race *in the test itself* (and ThreadSanitizer would report the test, not the barrier). This is also why the check allows `e + 1`.
- The slots are read and written with `memory_order_relaxed` **on purpose**. They then add no ordering of their own, and "slot ≥ e" can hold only because the barrier ordered the write before the read. With the default `seq_cst` the test would lend its own ordering to a barrier that lacks it: the episode check caught mutant 4 in 8 of 20 runs with relaxed slots and in 0 of 20 runs with `seq_cst` slots (in 2 of those 20 runs the handoff check, whose slots are plain variables, caught it instead).
- **Handoff check: no, deliberately.** With a second barrier before the next write, a correct barrier orders every write before every read, so plain variables are race-free exactly when the barrier keeps its promise. This turns ThreadSanitizer into a checker of the barrier's memory orders.

### 8.3 Results (`make test-part2`, 16 s; `make tsan-part2`, 5 s)

Plain build, episodes checked per variant (sum over the phases): `sr` 560 058, `sr_bo_spin` 480 195, `sr_bo_yield` 580 000, `sr_bo_sleep` 61 000, `sr_block` 188 000, `std_barrier` 188 000, `condvar` 61 000: all PASS. Only the oversubscribed phases of `sr` and `sr_bo_spin` (58 and 87 episodes in 1.5 s) and the `all-cores` phase of `sr_bo_spin` (20 108) ended on the time budget. Fuzzing build: `sr` 56 020, `sr_bo_spin` 56 020, `sr_bo_yield` 58 000, `sr_bo_sleep` 6 100, `sr_block` 18 800: all PASS. The plain test was repeated 8 more times without a failure. ThreadSanitizer build (`small` = 5 % of the sizes): all seven variants PASS with no report (see §9 for the one check that is skipped).

Linux container (g++ 14.2, aarch64, `-Wall -Wextra -Werror`, with and without `-fopenmp`): all nine variants PASS, including `pthread_barrier` and `omp_barrier`; fuzzing build PASS; ThreadSanitizer build PASS with no report (`omp_barrier` skipped, see §9).

### 8.4 Mutation testing (`make mutation-part2`, about 1.5 min)

Each mutant is a copy of `SenseBarrier<NoBackoff>` with one fault, in a marked section of `tests/test_barrier.cpp`, never compiled into a normal build. Each is built three times (plain, fuzzing, ThreadSanitizer); the stress tests are run 5 times.

| # | Mutant | Fault | Plain stress test | Stress + schedule fuzzing | ThreadSanitizer build |
|---|---|---|---|---|---|
| 1 | `single_flag` | no sense reversal: one flag, set by the last arriver, cleared by the last thread to leave | caught 5/5, **wrong episode** (at episode 2–3) | caught 5/5, wrong episode | no race report; checks: wrong episode |
| 2 | `flip_before_reset` | `sense_` flipped before `count_` is reset | caught 5/5 here, **hang** (after 24 199 … 161 522 episodes); 4/5 in an earlier run | caught 5/5, hang (after 2 … 10 episodes) | no race report; checks: hang |
| 3 | `nonatomic_count` | `load` + `store` instead of `fetch_add` | caught 5/5, **hang** (at episode 2) | caught 5/5, hang | no race report; checks: hang |
| 4 | `fetch_add_release_only` | `fetch_add(…, release)` instead of `acq_rel` | caught 2/5 (8/20 in a longer series), **wrong episode** | **survived 0/5** | **data race reported** (handoff check); checks passed |
| 5 | `thread_local_sense` | local sense is `thread_local` (shared by all barrier objects) | caught 5/5, **wrong value** in the handoff check; survives all single-barrier phases | caught 5/5, wrong value | data race reported; checks: wrong value |

Findings:

1. **No mutant survives everything, but no single technique catches everything.** ThreadSanitizer reports nothing for mutants 1–3: all their shared variables are atomic, so there is no data race in the C++ sense, only a wrong algorithm. Conversely, mutant 4 is a pure memory-order fault that the stress test catches only sometimes and schedule fuzzing never (the delays hide the reordering window); ThreadSanitizer catches it every time through the handoff check.
2. **Mutant 2 was the weak spot of the plain stress test.** Its window is one instruction wide (the last arriver must be preempted between two adjacent stores). The plain test needed tens of thousands of episodes and missed it in one of the first five runs; that was on a loaded machine with frequent preemption, and an idle machine will preempt less. What was missing was a way to delay a thread *between two steps of the protocol*; the test points plus schedule fuzzing provide it and the mutant now dies within 10 episodes in every run. The same test points are compiled into the production barrier in the fuzzing build, so the comparison is fair: the correct code passes under the same perturbation.
3. **Mutant 5 justifies the handoff check**: a test with a single barrier object cannot see a fault in how per-barrier state is kept.
4. **Mutant 4** shows that the `acquire` half of `fetch_add` is needed on real ARM hardware, not only on paper (§4), and that the test's own atomics must be `relaxed` to see it (§8.2).

Limits of the method: a stress test shows the presence of faults, not their absence; the detection of mutants 2 (plain) and 4 is probabilistic; the fuzzing delays are random, not an exhaustive exploration of interleavings.

## 9. ThreadSanitizer: known behaviour

- All own barriers, `condvar`, and on Linux `pthread_barrier` and `std_barrier` (libstdc++ implements `std::barrier` in the header) are clean.
- **`std::barrier` of libc++ (macOS) gives a false positive.** Its implementation is compiled into the system libc++ library, which is not instrumented, so ThreadSanitizer does not see the ordering the barrier creates and reports a data race on the plain variables of the handoff check (one report per run, in 5 of 5 runs). The episode check, which uses atomics only, is not affected. In a ThreadSanitizer build on libc++ the handoff check is therefore skipped for `std_barrier`, with a note in the output. `make tsan-part2-false-positive` runs it anyway and shows the report.
- **The OpenMP barrier (libgomp) gives false positives** for the same reason, already in the test's own launcher (the shared variable written in `single` and read after its implicit barrier). `omp_barrier` is skipped in ThreadSanitizer builds, with a note; the argument `tsan_uninstrumented` runs it anyway.
- ThreadSanitizer says nothing about mutants 1–3 (§8.4): silence is not a proof of correctness.

## 10. Benchmark and proposed parameter grid

`bench_barrier impl=<name> threads=<p> iters=<episodes>` prints one CSV row. Every thread runs `for (i < iters) barrier.wait(tid);` with nothing else in the loop. `ops` = `iters` = number of **episodes** (not `threads × iters`: one episode is one collective crossing; multiplying by the thread count would hide the O(n) cost that the study is about), so `ops_per_s` is the assignment's metric. `ok` = every thread completed `iters` episodes. The clock runs from the common start gate to the last join (`pc::timed_run`). `extra` records the compile-time backoff bounds for `sr_bo_sleep` and `sr_bo_spin`.

Grid: all implementations × threads ∈ {1, 2, 3, 4, 6, 8, 10, 12, 16, 20} × ≥ 7 repetitions, implementations interleaved inside each repetition.

Rates seen in the sanity runs (loaded machine, one run each, **not results**), in episodes per second:

| impl | p = 1 | 2 | 4 | 8 | 10 | 12 | 20 |
|---|---|---|---|---|---|---|---|
| `sr` | 3.6 × 10⁸ | 4.9 × 10⁶ | 2.4 × 10⁶ | 1.3 × 10⁶ | 6.2 × 10⁵ | 210 | 43 |
| `sr_bo_spin` | 4.2 × 10⁸ | 4.6 × 10⁶ | 8.0 × 10⁵ | 1.2 × 10⁵ | 2.9 × 10⁴ | 243 | 40 |
| `sr_bo_yield` | 4.2 × 10⁸ | 1.0 × 10⁷ | 4.6 × 10⁴ | 1.6 × 10⁴ | 6.6 × 10⁴ | 5.5 × 10³ | 2.2 × 10³ |
| `sr_bo_sleep` | 7.4 × 10⁷ | 3.3 × 10⁵ | 1.6 × 10⁵ | 5.5 × 10⁴ | 5.0 × 10⁴ | 4.3 × 10⁴ | 3.0 × 10⁴ |
| `sr_block` | 4.4 × 10⁷ | 8.7 × 10⁶ | 2.8 × 10⁶ | 1.9 × 10⁵ | 6.0 × 10⁴ | 5.6 × 10⁴ | 2.8 × 10⁴ |
| `std_barrier` | 3.4 × 10⁷ | 5.9 × 10⁶ | 1.0 × 10⁵ | 4.5 × 10⁴ | 3.5 × 10⁴ | 4.4 × 10⁴ | 1.8 × 10⁴ |
| `condvar` | 1.2 × 10⁸ | 5.3 × 10⁵ | 1.8 × 10⁵ | 5.1 × 10⁴ | 4.4 × 10⁴ | 3.9 × 10⁴ | 2.4 × 10⁴ |

The rates span seven orders of magnitude and depend on the machine and its load (the `sr_bo_yield` row is erratic because a yield hands the core to whatever else is running), so a fixed `iters` cannot give 0.1–0.5 s everywhere. Proposal:

1. **Calibrate once per (impl, threads)**: a pilot run with a small `iters` (2 000; 50 for `sr` and `sr_bo_spin` when threads > cores; 1 000 000 for threads = 1), then `iters = clamp(0.25 s × pilot ops_per_s, 50, 50 000 000)`, rounded to two significant digits. Keep this `iters` for all repetitions of that configuration, so that the rows are comparable.
2. **Fallback table without calibration** (episodes), from the rates above:

| impl | p = 1 | 2 | 3–4 | 6–8 | 10 | > cores (12, 16, 20) |
|---|---|---|---|---|---|---|
| `sr` | 50 000 000 | 1 000 000 | 500 000 | 300 000 | 150 000 | 50 |
| `sr_bo_spin` | 50 000 000 | 1 000 000 | 200 000 | 30 000 | 10 000 | 50 |
| `sr_bo_yield` | 50 000 000 | 2 000 000 | 200 000 | 100 000 | 100 000 | 1 000 |
| `sr_bo_sleep` | 20 000 000 | 80 000 | 40 000 | 15 000 | 12 000 | 8 000 |
| `sr_block` | 10 000 000 | 2 000 000 | 500 000 | 50 000 | 15 000 | 8 000 |
| `std_barrier`, `omp_barrier` | 10 000 000 | 1 000 000 | 30 000 | 12 000 | 10 000 | 5 000 |
| `condvar`, `pthread_barrier` | 20 000 000 | 100 000 | 40 000 | 12 000 | 10 000 | 6 000 |

3. **Per-run timeout**: 30 s for `sr` and `sr_bo_spin` when threads > cores (one episode can cost a whole scheduler time slice, and the start gate of `timed_run` spins as well); 10 s for everything else. A run that hits the timeout is recorded as a timeout (missing point), never as zero.
4. With the Makefile's OpenMP detection, `list` shows `omp_barrier` only where it was compiled in; the runner should take the implementation list from `bench_barrier list`.

## 11. Not verified

- **Linux x86-64**: only Linux aarch64 (container, g++ 14.2) was built and tested. The `_mm_pause` path of `platform.hpp` and 64-byte cache lines on x86 were not exercised.
- **OpenMP on macOS**: Apple clang rejects `-fopenmp` here, so `omp_barrier` was compiled and run only in the Linux container.
- **Mutation table on Linux**: only one plain and one fuzzing run per mutant in the container (same verdicts as on macOS for mutants 1, 2, 3 and 5; mutant 4 survived both and was reported by ThreadSanitizer). `make mutation-part2` itself was not run there (the container has no `make`; the sources were compiled with the Makefile's flags by hand).
- **An idle machine**: every rate in this file, and the detection rates of mutants 2 and 4, come from a loaded machine.
