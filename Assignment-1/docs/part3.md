# Part 3 — Concurrent counters: implementation notes

Raw material for the report. Everything stated as "observed" was seen on the development machine
(Apple M5, 4 performance + 6 efficiency cores, macOS, Apple clang 21.0.0, `-std=c++20 -O2`) on
14/10/2026 **while other builds and tests were running on the same machine** (1-minute load average
between 3 and 50). The timings below were used only to choose parameters. They are not results.

| File | Content |
|---|---|
| `include/counter.hpp` | the counters (`pc::counter_lock<L>`, `pc::counter_cas<Policy, Order>`, `pc::counter_faa_ord<Order>`) |
| `tests/test_counter.cpp` | correctness test, and the deliberately broken counters used for mutation testing |
| `bench/bench_counter.cpp` | benchmark, one CSV row per run |
| `mk/part3.mk` | `make test-part3`, `make tsan-part3`, `make mutation-part3` |

## 1. Specification

Every counter starts at 0 and has two operations:

- `long increment()` adds 1 and returns the value the counter had **before** this increment;
- `long get()` returns the current value.

If `increment()` is called M times in total, by any number of threads, the M calls return the
values 0, 1, …, M−1, each exactly once, and `get()` then returns M. Moreover, if one call has
returned before another one is made, the first call returns the smaller value (the counter is
linearizable: every increment appears to happen at one instant between its call and its return).

Returning the old value costs nothing: all three primitives produce it anyway. It is what allows
the test to check far more than the final value.

## 2. The implementations

| Kind | Type | Benchmark name | Progress guarantee |
|---|---|---|---|
| mutex lock | `counter_lock<L>`, `L` = any type with `acquire()` / `release()` | `lock_<lock name>`, one per lock of Part 1 | that of the lock (blocking) |
| compare-and-swap | `counter_cas<Policy, Order>` | `cas` (no backoff), `cas_spin`, `cas_yield`, `cas_sleep` (the assignment's sample backoff), `cas_seqcst` | lock-free: some thread always succeeds, a given thread may fail any number of times |
| fetch-and-add | `counter_faa_ord<Order>` | `faa`, `faa_seqcst` | wait-free: one instruction |

`bench_counter list` on this machine prints 19 names: `faa faa_seqcst cas cas_seqcst cas_spin
cas_yield cas_sleep lock_tas lock_ttas lock_mcs lock_tas_bo_sleep lock_tas_bo_spin
lock_tas_bo_yield lock_ttas_bo_sleep lock_ttas_bo_spin lock_ttas_bo_yield lock_std_mutex
lock_pthread_mutex lock_os_unfair_lock`. On Linux `lock_os_unfair_lock` is replaced by
`lock_pthread_spinlock`, and `lock_omp_lock` is added when the compiler has OpenMP. The lock list
comes from the macro `PC_FOR_EACH_LOCK` of `include/lock.h`, so a lock added in Part 1 appears here
without any change. If `lock.h` is missing, only `lock_std_mutex` is built (`__has_include`).

**CAS increment.** Read the value, then `compare_exchange_weak(old, old + 1)`. On failure the CAS
has stored the value it saw into `old`; the thread calls `Policy::pause()` ("a waiting time before
retrying a failed increment attempt", as the assignment puts it) and tries again. The policies are
those of `backoff.hpp`: `NoBackoff` (one relax hint), `SpinBackoff` (random number of relax hints
in a window that doubles), `YieldBackoff`, `SleepBackoff` (the code printed in the assignment:
`sleep_for(delay)`, `delay *= 2` up to `MAX_DELAY`). A new policy object is created for each
increment, so the delay starts again from its minimum.

**Failed attempts.** `counter_cas<…>::failed_attempts()` returns the number of failed CAS attempts
of the calling thread. The count is a `thread_local` variable: it costs one private addition on the
failure path and nothing on the success path. A shared atomic statistic would add a second
contended cache line and change what is being measured. Each benchmark thread reads its own count
at the end of its loop; the sum is the field `cas_fail` of the CSV row.

**Memory layout.** The atomic value is `alignas(pc::kCacheLine)` (128 bytes on Apple Silicon, 64
elsewhere), which also pads the object to a whole number of cache lines: nothing else can share
its line. In `counter_lock` the lock and the value are on two different lines. Reason: waiting
threads of a spinlock keep reading the lock word; with the value on the same line, each write to
the value would invalidate their copy. Cost: the lock holder touches two lines instead of one.

## 3. Memory orders

Default: `memory_order_relaxed` for every atomic operation of `counter_cas` and `counter_faa`.

Two properties must be kept apart.

1. **Atomicity of the read-modify-write.** `fetch_add` and a successful `compare_exchange` are
   atomic with *every* memory order: no other write to the same variable falls between the read
   and the write. In addition the C++ standard keeps one total order of all writes to a given
   atomic variable (its modification order), again with every memory order. These two facts give
   the specification of section 1: M increments return 0 … M−1 once each, and a thread's own
   results increase. No weaker order than relaxed exists, and nothing in the counter breaks with it.
2. **Ordering against other memory locations.** This is all that acquire, release and seq_cst
   add. The counter guards no data except its own value, so it does not need it.

| Operation | Order | Why it is enough | When a stronger order would be needed |
|---|---|---|---|
| `fetch_add(1)` | relaxed | atomic by itself | only if the counter is used to *publish other data* (a writer fills slot `k`, then increments; a reader that sees the count reads slot `k`): then release on the increment and acquire on `get()` |
| first `load` of the CAS loop | relaxed | only a guess; the CAS checks it | never |
| CAS, success | relaxed | as `fetch_add` | as `fetch_add` |
| CAS, failure | relaxed | a failed CAS only refreshes the guess | never |
| `get()` | relaxed | returns a value the counter really had; exact once the incrementing threads have been joined (`join` creates the happens-before edge) | as `fetch_add` |
| `counter_lock` | no atomics of its own | the value is a plain `long`; its visibility from one lock owner to the next comes entirely from the lock's release → acquire pair | here a weaker order *does* break the counter: a lock that releases with `relaxed` (mutant `TAS_RELEASE_RELAXED` of Part 1) gives a broken counter on ARM |

Why relaxed still respects real-time order whenever a program can tell: suppose thread B *knows*
that A's increment has finished, i.e. there is a happens-before path from A's increment to B's
(a join, a lock, an acquire/release pair). The coherence rules of the standard then force B's
increment after A's in the modification order, so B gets the larger value. Without such a path B
has no way to know that A "was first", and the two may be ordered either way.

What stronger orders cost is measurable only on ARM: seq_cst selects `ldaddal` / `casal` / `ldar`
instead of `ldadd` / `cas` / `ldr` (section 4). On x86-64 the generated instructions are the same
for both orders. The order is a template parameter so that the benchmark can measure the
difference: `faa_seqcst` and `cas_seqcst` are the same code with seq_cst.

## 4. What an increment costs at the hardware level

Assembly inspected with `c++ -std=c++20 -O2 -S` on this machine (Apple clang 21.0.0, default
target `arm64-apple-darwin`, which defines `__ARM_FEATURE_ATOMICS`, i.e. the ARMv8.1 LSE atomic
instructions are used without any flag).

| Source | arm64 with LSE (the default here) | arm64 without LSE (`-Xclang -target-feature -Xclang -lse`) | x86-64 (same clang `-arch x86_64`; g++ 12.2) |
|---|---|---|---|
| `fetch_add(1, relaxed)` | `ldadd x8, x0, [x0]`: **one instruction, no loop** | loop `ldxr` / `add` / `stxr` / `cbnz` | `lock xaddq` |
| `fetch_add(1, seq_cst)` | `ldaddal` | loop `ldaxr` / `add` / `stlxr` / `cbnz` | `lock xaddq` (same) |
| `compare_exchange_weak` relaxed | `cas x10, x9, [x8]`, then `cmp` | `ldxr` / `cmp` / `stxr`, **one attempt**, `clrex` when the comparison fails | `lock cmpxchgq` |
| `compare_exchange_weak` seq_cst | `casal` | `ldaxr` / `stlxr` | `lock cmpxchgq` (same) |
| `load` relaxed / seq_cst | `ldr` / `ldar` | `ldr` / `ldar` | `movq` / `movq` (same) |

Consequences.

- **`fetch_add` on the M5 is one LSE instruction, not an LL/SC loop.** It cannot fail and has
  nothing to retry.
- **A failed weak CAS.** With LSE (`cas`) and on x86 (`lock cmpxchg`) the instruction compares and
  swaps in one step: it fails only if the value really differs, so "weak" behaves exactly like
  "strong" and **every counted `cas_fail` is a real conflict** with another thread's increment.
  On an LL/SC machine (ARMv8.0 cores such as Cortex-A53/A72, or any build without LSE) the weak
  CAS is one `ldxr` … `stxr` attempt. `stxr` fails whenever the exclusive reservation taken by
  `ldxr` was lost: another core wrote to the cache line, but also an interrupt or a context switch
  between the two instructions. In the latter cases the value is unchanged and the failure is
  *spurious*. `compare_exchange_strong` hides this with an inner retry loop; the counter does not
  need that, because it loops anyway. On such a machine `cas_fail` therefore counts conflicts
  plus spurious failures.
- **Why none of them scales.** Every increment, whatever the primitive, must own the cache line of
  the counter exclusively. With p threads the line travels from core to core, and the increments
  are served one at a time at the speed of that transfer. The throughput limit is the line, not
  the number of cores. In the sanity runs one increment took about 2 ns with one thread and 6 to
  30 ns (all threads together, per increment) with 2 to 10 threads.
- **CAS versus fetch-and-add.** A CAS increment is two accesses, `ldr` then `cas`, and the line
  can change hands between them. A failed CAS has paid for the transfer of the line and made no
  progress. Fetch-and-add does both in one instruction and never wastes a transfer.
- **Lock.** At least three writes on two cache lines per increment (take the lock, write the
  value, release the lock), plus the waiting discipline of the lock.
- The failure path of the CAS counter reaches the `thread_local` count through one call of the
  TLS accessor on macOS (once per increment that fails, the address is then kept in a register);
  on Linux x86-64 it is a single `addq $1, %fs:…`.

Not verified here: g++ on **Linux arm64**. GCC there defaults to `-moutline-atomics`: the atomic
operation becomes a call to a libgcc helper (`__aarch64_ldadd8_relax`, `__aarch64_cas8_relax`, …)
that chooses LSE or LL/SC at run time. Check with `g++ -O2 -S` on such a machine, and try
`-march=armv8.1-a` (or `-mcpu=native`) to get the inline LSE instruction.

## 5. Correctness

### 5.1 Strategy (`tests/test_counter.cpp`)

Every thread keeps every value returned by `increment()`. After all threads have been joined, with
M = number of calls made:

| Check | Meaning of a failure |
|---|---|
| `[final]` `get() == M` | an update was lost |
| `[duplicate]` no value returned twice | two increments read the same state |
| `[missing]` every value 0 … M−1 returned | a value was skipped; with `[duplicate]`: the returned values are exactly {0, …, M−1} |
| `[order]` the values returned to one thread increase strictly | a thread saw the counter go backwards |
| `[real-time]` (separate run) if A returned before B was called, value(A) < value(B) | values are handed out in an order that contradicts the order of the calls |

Runs for each counter: 1 thread (sequential behaviour), `hw` threads, 2 × `hw` threads
(oversubscription: threads are preempted in the middle of an increment), then the real-time run
with `hw` and 2 × `hw` threads. All threads wait at the start gate of `pc::timed_run` and start
together. `small` divides the sizes by 16 (ThreadSanitizer build); `only=<name>` tests one counter;
`verbose` prints one line per run.

**Why this is the linearizability evidence for a counter.** A history is linearizable if its
operations can be put in one sequence that (1) is a legal sequential history of the object and
(2) keeps the order of any two operations that did not overlap in time. For a counter whose
`increment()` returns the old value, a legal sequential history is one in which the k-th increment
returns k−1. So (1) holds **if and only if** the returned values are exactly {0, …, M−1}, and the
only candidate sequence is "sorted by returned value". `[duplicate]` + `[missing]` decide (1)
exactly. Condition (2) then says: whenever A finished before B began, value(A) < value(B).
`[order]` checks this for all pairs inside one thread. `[real-time]` checks it for pairs in
different threads.

**The real-time run.** Each increment is bracketed by two ticks of a logical clock, a seq_cst
`std::atomic<long>`: `inv = clock.fetch_add(1); value = c.increment(); resp = clock.fetch_add(1);`.
`resp(A) < inv(B)` proves that A returned before B was called. The check sorts by value and looks
for an increment that was called after a larger value had already been returned (one pass,
O(M)). For a correct counter such a pair cannot exist, so the check gives no false alarm. A wall
clock was not used: its resolution on this machine (41 ns measured, a 24 MHz timebase) is longer
than an increment.

**Hangs.** A watchdog thread looks at the clock every 20 ms. After a soft budget (1.5 s per run,
3 s in the TSan build) it sets a stop flag; the workers finish early and the checks are applied to
the increments actually made (M is counted, not assumed). After a hard limit (60 s) it prints the
counter and the run that is stuck and ends the process with exit code 3. The budget bounds runs
that are slow but progressing; the hard limit catches a thread that never returns from
`increment()` (deadlock, lost hand-over).

### 5.2 What the test does not prove

- It examines the executions that happened, not all interleavings. Passing is evidence, not proof.
- `[real-time]` comes from an instrumented run: two extra contended `fetch_add` per increment
  change the timing and reduce the overlap between increments, so this run stresses the counter
  less than the plain run. The two runs are complementary.
- `get()` is only checked after the threads have been joined, not while they are incrementing.
- Progress (lock-freedom, wait-freedom, fairness, starvation) is not tested, apart from a
  complete hang.
- Behaviours of a weak memory model are tested only as far as this CPU exhibits them.
- ThreadSanitizer finds **data races**. An increment made of two separately atomic steps
  (mutants `load_store`, `cas_no_retry`) has no data race, so TSan is silent by design; only the
  value checks find it. Conversely TSan finds a race even in a run where the values happen to
  come out right.
- When the time budget cuts a run, fewer increments are checked. This happens for `lock_mcs`
  with more runnable threads than cores (below).

### 5.3 The trap: the optimiser can hide a non-atomic counter

A loop of non-atomic increments is a data race, i.e. undefined behaviour, so the compiler may
transform it as if no other thread existed. Observed at `-O2` on this machine (assembly read with
`-S`; 10 threads × 128 000 increments, 200 repetitions, counter = a plain `long`):

| Loop body | Generated code | Final value wrong |
|---|---|---|
| `c.increment();` result discarded | **one** `ldr` / `add x8, x8, n` / `str` for the whole loop | 183 of 200 runs with 10 threads, **2 of 200 with 2 threads** (200 of 200 at `-O0`) |
| the same, threads started with plain `std::thread` (no start gate), 1 000 000 increments | the same | **0 of 200** with 10 threads, 3 of 200 with 2 threads (200 of 200 at `-O0`) |
| `out[i] = c.increment();`, `out` is `long*` like the counter | load and store in every iteration (the compiler must assume that `out[i]` may be the counter) | 200 of 200 |
| `out[i] = (int)c.increment();`, `out` is `int*` | folded again: one load, a vectorised fill of `out`, one store (strict aliasing: an `int` store cannot change a `long`) | 200 of 200 here, but only because the single load and the single store are now separated by the whole loop |
| `out[i] = pc::opaque(&c)->increment();` | load and store in every iteration, whatever the types | 200 of 200 |

So the textbook test (start threads, increment, compare the final value) **passes on a counter
with no synchronisation at all** at `-O2`, and fails at `-O0`. A detail found on the way: when the
loop bound was captured by reference (`[&]`), the compiler could not fold the loop, because the
store to the counter might change the bound; capturing it by value made the bug invisible.

What the test does about it: every returned value is stored and checked, the buffer has the same
type as the counter (`long`), and the address of the counter goes through `pc::opaque()` in every
iteration, which forces a real load and store per increment for any counter type and generates no
instruction. Verified in the assembly of the mutant build. The real counters are not affected:
compilers do not merge atomic operations or lock calls.

### 5.4 Results

`make test-part3` (about 15 s): all 19 counters PASS; 13.3 million increments checked for each
atomic counter, 3.3 million for each lock-based counter and for `cas_sleep`.
`make tsan-part3` (about 30 s): all 19 PASS, no ThreadSanitizer report.
Also built with g++ 12.2 (`-std=c++20 -O2 -Wall -Wextra`) in a Linux x86-64 container running under
emulation on the same laptop: all 20 counters there PASS (including `lock_pthread_spinlock` and
`lock_omp_lock`), and the five mutants are caught by the same checks. That is a compile and
functional check only, not a second hardware platform; ThreadSanitizer was not run there.

`lock_mcs` reaches the time budget whenever there are more runnable threads than cores (always
with 2 × `hw` threads; also with `hw` threads while the machine is busy): between 0.1 and 2 ms per
increment were observed instead of about 0.2 µs, because the lock is handed to a thread that is
not running. The checks pass on the increments made. This is the known weakness of queue locks
under preemption and belongs to the discussion of Part 1.

`make mutation-part3` (5 mutants, each: 3 runs of the normal build, 1 run of the TSan build):

| Mutant | Bug | Value checks | Checks that fired | TSan |
|---|---|---|---|---|
| `plain_long` | `long`, `value_++`, no synchronisation | caught 3/3 | final, duplicate, missing, order | data race |
| `load_store` | atomic `load`, then atomic `store` | caught 3/3 | final, duplicate, missing, order | no report |
| `cas_no_retry` | one CAS, result ignored | caught 3/3 | final, duplicate, missing | no report |
| `lock_read_outside` | value read before `acquire()`, written inside | caught 3/3 | final, duplicate, missing, order | data race |
| `block_reserve` | each thread reserves 8 values with one `fetch_add` and hands them out privately | caught 3/3 | **real-time only** | no report |

Reading the table: TSan sees two of the five (the two that touch a non-atomic variable without
the lock). Three are visible only to the value checks, and `block_reserve` only to the real-time
check: its final value is right and its returned values are exactly {0, …, M−1}, increasing in
every thread. `[order]` may or may not fire in a given run of a racy mutant; the other columns
were stable over the runs made. The mutants are defined in the test file under `#if PC_MUTANT`,
never in `counter.hpp`. The TSan builds of the mutants are named `counter_muttsan_<name>` on
purpose: a name ending in `_tsan` would also match the generic `%_tsan` rule of the top-level
Makefile, and GNU make 3.81 (the version shipped with macOS) picks the first matching rule.

## 6. Benchmark (`bench/bench_counter.cpp`)

```
bench_counter impl=<name> threads=<p> iters=<I per thread> outside_work=<OW> n=<prime>
bench_counter list | header
```

Each thread does `iters` times: one `increment()`, then `outside_work` calls of `pc::is_prime(n)`.
The row has `part=counter`, `ops = threads × iters`, `seconds` = time from the common start to the
end of the last thread (`pc::timed_run`). `extra` holds `cas_fail:<total>` (CAS counters only) and
`thr_min_s` / `thr_max_s`, the loop time of the fastest and of the slowest thread.

`ok = 1` requires: final value = M; the sum of all returned values = M(M−1)/2 (each thread adds
the values it receives, so the optimiser cannot drop the increments; a counter that loses or
duplicates values is flagged in practice, but errors that cancel out in the sum would pass: the
exact check is the one of the test program); every `is_prime` sum = number of calls.
`n` must be prime, otherwise the program refuses to run. One template instantiation per counter
type; no virtual call in the loop (checked in the assembly: the `faa` loop is `ldadd`, add to the
sum, loop counter).

### Sanity observations (noisy, for choosing parameters only)

Time per increment = `seconds / ops`, `outside_work = 0`:

| Counter | 1 thread | 2–10 threads | 20 threads |
|---|---|---|---|
| `faa`, `faa_seqcst` | 2 ns | 6–30 ns | 15 ns |
| `cas`, `cas_seqcst` | 2 ns | 4–60 ns, 0.07–1.4 failed attempts per increment | 40 ns |
| `cas_spin`, `cas_yield`, `cas_sleep` | 2–3 ns | 2–4 ns, under 0.01 failed attempts per increment | 3–4 ns |
| `lock_tas`, `lock_ttas` | 4 ns | 50–230 ns | 300–450 ns |
| `lock_*_bo_*` | 4 ns | 4–9 ns | 4–15 ns |
| `lock_std_mutex`, `lock_pthread_mutex`, `lock_os_unfair_lock` | 3–5 ns | 7–15 ns | 12 ns |
| `lock_mcs` | 4–6 ns | 165–305 ns | 0.1–2 ms (collapse) |

A point to settle with the real measurements: with `outside_work = 0` the backoff variants look as
fast as a single thread. `thr_min_s` shows why: the first thread finished all its 200 000
increments in 0.4 ms, at single-thread speed, while the others were waiting. Backoff then turns the
run into p sequential bursts. That minimises the total time, which is the assignment's metric, but
it is the opposite of fairness, and a run must be long enough for the effect not to be a start-up
artefact. With `outside_work = 8` the picture reverses for the sleep policy: `cas_sleep` with 10
threads took 0.86 s where `cas` took 0.27 s, and its slowest thread needed ten times as long as
its fastest. One `is_prime(101)` took about 47 ns.

## 7. Proposed parameter grid for the full study

- **impl**: all names of `bench_counter list`.
- **threads**: 1, 2, 3, 4, 6, 8, 10, 12, 16, 20 (as in Part 1; 4 = performance cores, 10 = all
  cores, above 10 = oversubscription).
- **outside_work**: 0, 1, 8 (and 64 if time allows: the regime where the counter should stop
  mattering); **n** = 101.
- **repetitions**: at least 7 per configuration, implementations interleaved in each round.

**iters.** The time per increment spans 2 ns to 2 ms, so no single value gives 0.1–0.5 s
everywhere. Proposal: choose `iters` per (impl, threads, outside_work) from a pilot run,
`iters ≈ 0.25 s / (seconds per thread-iteration of the pilot)`, rounded to two significant digits
and kept identical over the repetitions. If a fixed table is preferred, per thread:

| outside_work | Counters | threads = 1 | 2–4 | 6–10 | 12–20 |
|---|---|---|---|---|---|
| 0 | `faa*`, `cas`, `cas_seqcst` | 50 000 000 | 5 000 000 | 1 000 000 | 500 000 |
| 0 | CAS with backoff, `lock_*_bo_*`, library locks | 50 000 000 | 10 000 000 | 5 000 000 | 2 000 000 |
| 0 | `lock_tas`, `lock_ttas` | 50 000 000 | 1 000 000 | 200 000 | 50 000 |
| 0 | `lock_mcs` | 50 000 000 | 500 000 | 100 000 | 500 (expect a timeout) |
| 1 | all except `lock_mcs` above 10 threads | 2 000 000 | 500 000 | 200 000 | 200 000 |
| 8 | all except `lock_mcs` above 10 threads | 500 000 | 200 000 | 100 000 | 100 000 |

From the sanity runs these values should give about 0.05 to 0.6 s per run on this machine; they
have to be re-checked on every other platform. Analyse `seconds / ops` (time per increment) as
well as `seconds`, since `iters` differs between configurations; `cas_fail / ops` is the secondary measure asked for in the plan.

**Timeouts** (a timeout is a data point, "collapsed", not an error):

- default: 10 s per run (20 to 100 times the target duration);
- sleep-backoff variants (`cas_sleep`, `lock_*_bo_sleep`): 10 s is enough in every case seen;
  the worst was 3 times slower than the same counter without backoff;
- spinlocks without backoff, oversubscribed (`lock_tas`, `lock_ttas`, more than 10 threads): 10 s;
  they were 1.3 to 2 times slower per increment than with 10 threads;
- `lock_mcs` with more than 10 threads (or with 10 threads on a busy machine): 30 s, `iters` =
  500, at most two attempts per configuration.

## 8. Open points

- **Shared header, g++ 12 only:** `include/bench_common.hpp:28` (`kv_[a] = "1";`) triggers a
  `-Wstringop-overflow` warning inside libstdc++ with g++ 12.2 at `-O2` (a known false positive of
  that GCC version). It appears in every program that uses `pc::Args`; Apple clang 21 is silent.
  `kv_[a] = std::string("1");` is the usual way to avoid it. Not changed here.
- Linux arm64 with g++ (outline atomics) and real x86-64 hardware: not tested.
- Whether `faa_seqcst` / `cas_seqcst` differ measurably from the relaxed versions on the M5 can
  only be decided by the quiet measurement campaign; the sanity runs are too noisy to tell.
