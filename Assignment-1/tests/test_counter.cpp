// test_counter.cpp -- correctness tests for the concurrent counters of include/counter.hpp.
//
//   build/test_counter                 all counters, full sizes
//   build/test_counter small           small sizes (used for the ThreadSanitizer build)
//   build/test_counter only=cas_spin   one counter
//   build/test_counter verbose         also print one line per run
//   build/test_counter_tsan small tsan_uninstrumented
//                                      also test lock_omp_lock under ThreadSanitizer (skipped by
//                                      default: the OpenMP run-time is not instrumented)
//
// What is checked (details and limits: docs/part3.md)
// ---------------------------------------------------
// Every thread stores every value returned by increment(). After all threads have finished, with
// M = total number of increment() calls made:
//   [final]     get() == M                                   (no lost update)
//   [duplicate] no value was returned twice                  (no two increments saw the same state)
//   [missing]   every value 0 .. M-1 was returned            (with [duplicate]: the returned values
//                                                             are exactly the set {0, ..., M-1})
//   [order]     the values returned to ONE thread are strictly increasing
//   [real-time] (separate, instrumented run) if increment A returned before increment B was
//               called, then A's value is smaller than B's value
// Each counter is run with 1 thread, with as many threads as hardware threads, and with twice as
// many (oversubscription: threads get preempted in the middle of an increment).
//
// Hangs: a watchdog thread ends a run that is too slow (soft time budget: the worker threads stop
// early and the checks are applied to the increments actually made) and kills the program with a
// message if a run makes no progress at all (hard limit: deadlock or lost wake-up).
//
// Mutation testing: compiled with -DPC_MUTANT=PC_MUTANT_<name>, the program tests ONE
// deliberately broken counter instead of the real ones (make mutation-part3). The broken counters
// are defined in this file, not in counter.hpp, so they can never be used by accident.
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "backoff.hpp"
#include "bench_common.hpp"
#include "counter.hpp"
#include "platform.hpp"
#include "workload.hpp"

// The locks of Part 1. If lock.h is missing, the lock-based counter is tested with std::mutex only.
#if !defined(PC_MUTANT) && __has_include("lock.h")
#include "lock.h"
#endif

// PC_TSAN is defined when this file is compiled with -fsanitize=thread (gcc and clang differ).
#if defined(__SANITIZE_THREAD__)
#define PC_TSAN 1
#elif defined(__has_feature)
#if __has_feature(thread_sanitizer)
#define PC_TSAN 1
#endif
#endif

namespace {

// A minimal lock with the acquire()/release() interface, used when lock.h is not available and by
// the lock-based mutant.
struct std_mutex_adapter {
    std::mutex m;
    void acquire() { m.lock(); }
    void release() { m.unlock(); }
};

// ================================================================================================
// Deliberately broken counters (mutation testing). Compiled only with -DPC_MUTANT=...
// ================================================================================================
#define PC_MUTANT_plain_long        1
#define PC_MUTANT_load_store        2
#define PC_MUTANT_cas_no_retry      3
#define PC_MUTANT_lock_read_outside 4
#define PC_MUTANT_block_reserve     5

#if defined(PC_MUTANT)
#if PC_MUTANT == PC_MUTANT_plain_long
// No synchronisation at all: `value_++` is load, add, store. Two threads can load the same value
// (duplicate), and one of the two stores is then lost. It is also a data race (undefined
// behaviour), which is why the compiler may transform a loop of such increments: see the comment
// in stress_run().
struct mutant_counter {
    static constexpr const char* name = "plain_long";
    alignas(pc::kCacheLine) long value_ = 0;
    long increment() { return value_++; }
    long get() const { return value_; }
};
#elif PC_MUTANT == PC_MUTANT_load_store
// Every access is atomic, but the increment is TWO atomic operations. Another thread can
// increment between the load and the store. There is no data race in the C++ sense here, so
// ThreadSanitizer has nothing to report: only the value checks can find this bug.
struct mutant_counter {
    static constexpr const char* name = "load_store";
    alignas(pc::kCacheLine) std::atomic<long> value_{0};
    long increment() {
        const long old = value_.load();
        value_.store(old + 1);
        return old;
    }
    long get() const { return value_.load(); }
};
#elif PC_MUTANT == PC_MUTANT_cas_no_retry
// A CAS increment without the retry loop: when the CAS fails, the increment is silently dropped
// and the stale value is returned. (On an LL/SC machine compare_exchange_weak can also fail
// spuriously, so this counter could lose increments even with a single thread.)
struct mutant_counter {
    static constexpr const char* name = "cas_no_retry";
    alignas(pc::kCacheLine) std::atomic<long> value_{0};
    long increment() {
        const long old = value_.load();
        long expected = old;
        value_.compare_exchange_weak(expected, old + 1);   // BUG: the result is ignored
        return old;
    }
    long get() const { return value_.load(); }
};
#elif PC_MUTANT == PC_MUTANT_lock_read_outside
// The write is protected by the lock but the read is made before the lock is taken, so the
// critical section does not cover the whole read-modify-write.
struct mutant_counter {
    static constexpr const char* name = "lock_read_outside";
    alignas(pc::kCacheLine) std_mutex_adapter lock_;
    alignas(pc::kCacheLine) long value_ = 0;
    long increment() {
        const long old = value_;                           // BUG: read outside the lock
        lock_.acquire();
        value_ = old + 1;
        lock_.release();
        return old;
    }
    long get() {
        lock_.acquire();
        const long v = value_;
        lock_.release();
        return v;
    }
};
#elif PC_MUTANT == PC_MUTANT_block_reserve
// Each thread reserves a block of 8 consecutive values with one fetch_add and then hands them out
// privately. Nothing is lost and nothing is duplicated; the returned values are exactly
// {0, ..., M-1} when every thread makes a multiple of 8 increments. But the values no longer
// follow the order in which the increments happened: a thread can return 8 long before another
// thread returns 1. Only the [real-time] check can see this. (Such a counter is a legitimate
// design for unique ids; it is "broken" only with respect to the specification of counter.hpp.
// If the time budget ever cut a run short, the unused rest of a block would also show up as
// [missing]; the runs of this mutant take a few milliseconds, far below the budget.)
struct mutant_counter {
    static constexpr const char* name = "block_reserve";
    static constexpr long kBlock = 8;
    alignas(pc::kCacheLine) std::atomic<long> next_block_{0};
    static inline thread_local long next_ = 0;             // next value of this thread's block
    static inline thread_local long end_ = 0;              // one past the end of the block
    long increment() {
        if (next_ == end_) {
            next_ = next_block_.fetch_add(kBlock);
            end_ = next_ + kBlock;
        }
        return next_++;
    }
    long get() const { return next_block_.load(); }
};
#else
#error "unknown mutant: use -DPC_MUTANT=PC_MUTANT_<name>, names are listed above"
#endif
#endif  // PC_MUTANT

// ================================================================================================
// Watchdog. One helper thread looks at the clock every 20 ms.
//   soft deadline passed -> sets `stop`; the worker threads see it and finish early;
//   hard deadline passed -> the run is stuck (a thread never came back from increment()):
//                           print what was running and end the process with exit code 3.
// Everything is a std::atomic with static storage, so the detached thread never touches an
// object that has been destroyed.
// ================================================================================================
namespace watchdog {
constexpr double kNever = 1e300;
std::atomic<bool> stop{false};
std::atomic<double> soft_deadline{kNever};
std::atomic<double> hard_deadline{kNever};
std::atomic<const char*> counter_name{""};
std::atomic<const char*> run_kind{""};
std::atomic<int> run_threads{0};

void start() {
    std::thread([] {
        for (;;) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            const double t = pc::now_s();
            if (t > soft_deadline.load()) stop.store(true);
            if (t > hard_deadline.load()) {
                std::fprintf(stderr,
                             "\nFAIL  %s: WATCHDOG, the %s run with %d threads did not finish "
                             "(deadlock, lost wake-up or starvation)\n",
                             counter_name.load(), run_kind.load(), run_threads.load());
                std::fflush(nullptr);
                std::_Exit(3);
            }
        }
    }).detach();
}

void begin_run(const char* name, const char* kind, int threads, double soft_s, double hard_s) {
    stop.store(false);
    counter_name.store(name);
    run_kind.store(kind);
    run_threads.store(threads);
    const double t = pc::now_s();
    hard_deadline.store(t + hard_s);
    soft_deadline.store(t + soft_s);
}

void end_run() {
    soft_deadline.store(kNever);
    hard_deadline.store(kNever);
}
}  // namespace watchdog

// ================================================================================================
// The checks
// ================================================================================================
struct Verdict {
    long made = 0;              // M: increment() calls actually made
    long final_value = 0;       // get() after all threads have finished
    long out_of_range = 0;      // returned values outside 0 .. M-1
    long duplicate = 0;         // returned values that had already been returned
    long missing = 0;           // values of 0 .. M-1 that were never returned
    long not_increasing = 0;    // places where a thread got a value <= its previous value
    long realtime = -1;         // real-time order violations; -1 = not checked in this run
    long first_duplicate = -1;  // examples for the failure message
    long first_missing = -1;
    double seconds = 0;

    bool final_ok() const { return final_value == made; }
    bool set_ok() const { return out_of_range == 0 && duplicate == 0 && missing == 0; }
    bool ok() const { return final_ok() && set_ok() && not_increasing == 0 && realtime <= 0; }
};

// Which kinds of check have failed so far (summary line of the mutant builds).
struct Fired {
    bool final_value = false, duplicate = false, missing = false, order = false, realtime = false;
} g_fired;

// values[t] = the values returned to thread t, in the order of its calls.
Verdict check_values(const std::vector<std::vector<long>>& values, long final_value) {
    Verdict v;
    v.final_value = final_value;
    for (const auto& mine : values) v.made += static_cast<long>(mine.size());

    // seen[x] becomes true when value x is met for the first time.
    std::vector<bool> seen(static_cast<std::size_t>(v.made), false);
    for (const auto& mine : values) {
        long previous = -1;
        for (long x : mine) {
            if (x <= previous) ++v.not_increasing;
            previous = x;
            if (x < 0 || x >= v.made) {
                ++v.out_of_range;
            } else if (seen[static_cast<std::size_t>(x)]) {
                if (v.duplicate++ == 0) v.first_duplicate = x;
            } else {
                seen[static_cast<std::size_t>(x)] = true;
            }
        }
    }
    for (long x = 0; x < v.made; ++x) {
        if (!seen[static_cast<std::size_t>(x)]) {
            if (v.missing++ == 0) v.first_missing = x;
        }
    }
    return v;
}

void print_failure(const char* name, const char* kind, int threads, const Verdict& v) {
    std::printf("FAIL  %s: %s run, %d threads, %ld increments:", name, kind, threads, v.made);
    if (!v.final_ok()) std::printf(" [final] get() = %ld, expected %ld;", v.final_value, v.made);
    if (v.duplicate) std::printf(" [duplicate] %ld (e.g. value %ld);", v.duplicate, v.first_duplicate);
    if (v.missing) std::printf(" [missing] %ld (e.g. value %ld);", v.missing, v.first_missing);
    if (v.out_of_range) std::printf(" [missing] %ld values outside 0..M-1;", v.out_of_range);
    if (v.not_increasing) std::printf(" [order] %ld values not above the thread's previous value;", v.not_increasing);
    if (v.realtime > 0) std::printf(" [real-time] %ld increments started after a larger value had been returned;", v.realtime);
    std::printf("\n");
}

void record(const Verdict& v) {
    if (!v.final_ok()) g_fired.final_value = true;
    if (v.duplicate) g_fired.duplicate = true;
    if (v.missing || v.out_of_range) g_fired.missing = true;
    if (v.not_increasing) g_fired.order = true;
    if (v.realtime > 0) g_fired.realtime = true;
}

// ================================================================================================
// Run 1: stress. All threads start together (start gate of pc::timed_run) and call increment()
// as fast as they can; nothing but the store of the returned value is between two calls.
// ================================================================================================
template <class Counter>
Verdict stress_run(const char* name, const char* kind, int threads, long iters, double soft_s,
                   double hard_s) {
    Counter counter;
    // One result buffer per thread, allocated and zero-filled before the threads start, so the
    // measured loop does no allocation and takes no page fault.
    std::vector<std::vector<long>> values(static_cast<std::size_t>(threads),
                                          std::vector<long>(static_cast<std::size_t>(iters)));
    std::vector<long> made(static_cast<std::size_t>(threads), 0);

    watchdog::begin_run(name, kind, threads, soft_s, hard_s);
    const double seconds = pc::timed_run(threads, [&](int tid) {
        long* out = values[static_cast<std::size_t>(tid)].data();
        long i = 0;
        for (; i < iters; ++i) {
            // Soft time budget.
            // relaxed: the flag is only a request to stop; seeing it a little later is fine.
            if (watchdog::stop.load(std::memory_order_relaxed)) break;
            // The address of the counter goes through pc::opaque() on every iteration, so the
            // compiler has to treat each increment() as a call on an unknown counter. Without
            // this, a NON-atomic counter (mutant plain_long) could legally be kept in a register
            // for the whole loop: one load before the loop, one store after it. The race would
            // then exist only for a few nanoseconds per thread instead of on every increment.
            // For the real counters this changes nothing (atomic operations and lock calls are
            // never merged), and opaque() generates no instruction.
            Counter* c = pc::opaque(&counter);
            out[i] = c->increment();          // every returned value is kept and checked
        }
        made[static_cast<std::size_t>(tid)] = i;
    });
    watchdog::end_run();

    for (int t = 0; t < threads; ++t)
        values[static_cast<std::size_t>(t)].resize(static_cast<std::size_t>(made[static_cast<std::size_t>(t)]));
    Verdict v = check_values(values, counter.get());
    v.seconds = seconds;
    return v;
}

// ================================================================================================
// Run 2: real-time order. The stress run shows that the returned values are a permutation of
// 0 .. M-1; it says nothing about WHEN each value was handed out. Here every increment is
// bracketed by two ticks of a logical clock:
//
//     inv   = clock.fetch_add(1);       // before the call  ("invocation")
//     value = counter.increment();
//     resp  = clock.fetch_add(1);       // after the return ("response")
//
// The clock is a seq_cst atomic, so all ticks of all threads are totally ordered and each tick is
// ordered with the increment next to it. If resp(A) < inv(B), increment A had returned before
// increment B was called, and a correct counter must give A the smaller value. The check below
// looks for a pair that violates this. It cannot raise a false alarm: for a correct counter such
// a pair does not exist.
// The two extra fetch_add per increment slow the threads down and reduce the overlap between
// increments, which is why this is a separate run and not part of the stress run.
// ================================================================================================
struct Op {
    long inv, value, resp;
};

template <class Counter>
Verdict realtime_run(const char* name, int threads, long iters, double soft_s, double hard_s) {
    Counter counter;
    alignas(pc::kCacheLine) std::atomic<long> clock{0};
    std::vector<std::vector<Op>> ops(static_cast<std::size_t>(threads),
                                     std::vector<Op>(static_cast<std::size_t>(iters)));
    std::vector<long> made(static_cast<std::size_t>(threads), 0);

    watchdog::begin_run(name, "real-time", threads, soft_s, hard_s);
    const double seconds = pc::timed_run(threads, [&](int tid) {
        Op* out = ops[static_cast<std::size_t>(tid)].data();
        long i = 0;
        for (; i < iters; ++i) {
            if (watchdog::stop.load(std::memory_order_relaxed)) break;   // as in stress_run
            Counter* c = pc::opaque(&counter);
            out[i].inv = clock.fetch_add(1);      // default order = seq_cst, on purpose (see above)
            out[i].value = c->increment();
            out[i].resp = clock.fetch_add(1);
        }
        made[static_cast<std::size_t>(tid)] = i;
    });
    watchdog::end_run();

    // First the same checks as in the stress run.
    std::vector<std::vector<long>> values(static_cast<std::size_t>(threads));
    for (int t = 0; t < threads; ++t) {
        const auto ut = static_cast<std::size_t>(t);
        ops[ut].resize(static_cast<std::size_t>(made[ut]));
        values[ut].reserve(ops[ut].size());
        for (const Op& op : ops[ut]) values[ut].push_back(op.value);
    }
    Verdict v = check_values(values, counter.get());
    v.seconds = seconds;
    if (!v.set_ok()) return v;                    // values are not 0 .. M-1: cannot index by value

    // inv_of[x], resp_of[x] = the two ticks of the increment that returned x.
    const auto m = static_cast<std::size_t>(v.made);
    std::vector<long> inv_of(m), resp_of(m);
    for (const auto& mine : ops)
        for (const Op& op : mine) {
            inv_of[static_cast<std::size_t>(op.value)] = op.inv;
            resp_of[static_cast<std::size_t>(op.value)] = op.resp;
        }
    // Walk from the largest value down. earliest_resp_above = the earliest response among all
    // increments with a LARGER value. If that response is before the invocation of the increment
    // that returned x, a larger value was handed out, completely, before x was even requested.
    v.realtime = 0;
    long earliest_resp_above = std::numeric_limits<long>::max();
    for (std::size_t x = m; x-- > 0;) {
        if (earliest_resp_above < inv_of[x]) ++v.realtime;
        if (resp_of[x] < earliest_resp_above) earliest_resp_above = resp_of[x];
    }
    return v;
}

// ================================================================================================
// Sizes. `iters` is per thread. The numbers are chosen so that a run takes well under a second on
// the development machine; the soft time budget covers slower machines and the cases that are slow
// by nature (a queue lock with more threads than cores).
// ================================================================================================
enum class Speed { fast, medium };

struct Config {
    bool small = false;
    bool verbose = false;
    std::string only;           // empty = all counters
    int hw = 1;                 // hardware threads
    double soft_s = 1.5;        // soft time budget of one run
    double hard_s = 60.0;       // hard limit of one run (watchdog kills the program)
};

// Every count used below is a multiple of 8, the block size of mutant block_reserve.
long base_iters(Speed speed, bool small) {
    const long n = speed == Speed::fast ? 512000     // counters made of atomic instructions
                                        : 128000;    // lock-based counters, sleep-based backoff
    return small ? n / 16 : n;                       // 32000 / 8000 under ThreadSanitizer
}

// Runs all tests for one counter type and prints one PASS/FAIL line.
template <class Counter>
bool test_counter(const char* name, Speed speed, const Config& cfg) {
    if (!cfg.only.empty() && cfg.only != name) return true;
    const long iters = base_iters(speed, cfg.small);
    const int over = 2 * cfg.hw;                 // oversubscription: 2 threads per hardware thread

    struct Run {
        const char* kind;
        int threads;
        long iters;
        bool realtime;
    };
    const Run runs[] = {
        {"single-thread", 1, iters, false},
        {"stress", cfg.hw, iters, false},
        {"oversubscribed", over, iters / 2, false},
        {"real-time", cfg.hw, iters / 4, true},
        {"real-time oversubscribed", over, iters / 8, true},
    };

    bool ok = true;
    double seconds = 0;
    long checked = 0;
    bool cut = false;
    for (const Run& r : runs) {
        const Verdict v = r.realtime
            ? realtime_run<Counter>(name, r.threads, r.iters, cfg.soft_s, cfg.hard_s)
            : stress_run<Counter>(name, r.kind, r.threads, r.iters, cfg.soft_s, cfg.hard_s);
        record(v);
        seconds += v.seconds;
        checked += v.made;
        if (v.made < static_cast<long>(r.threads) * r.iters) cut = true;
        if (cfg.verbose)
            std::printf("      %-22s %-25s threads=%-3d increments=%-9ld %.3f s%s\n", name, r.kind,
                        r.threads, v.made, v.seconds, v.ok() ? "" : "  <-- FAIL");
        if (!v.ok()) {
            print_failure(name, r.kind, r.threads, v);
            ok = false;
        }
    }
    std::printf("%s  %-22s threads 1 / %d / %d, %9ld increments checked, %.2f s%s\n",
                ok ? "PASS" : "FAIL", name, cfg.hw, over, checked, seconds,
                cut ? "  (time budget reached: fewer increments than planned)" : "");
    std::fflush(stdout);
    return ok;
}

}  // namespace

int main(int argc, char** argv) {
    const pc::Args args(argc, argv);
    Config cfg;
    cfg.small = args.has("small");
    cfg.verbose = args.has("verbose");
    cfg.only = args.str("only", "");
    cfg.hw = static_cast<int>(pc::hw_threads());
    if (cfg.small) cfg.soft_s = 3.0;             // ThreadSanitizer slows everything down
    watchdog::start();

    std::printf("test_counter: %u hardware threads, %s sizes, time budget %.1f s per run\n",
                pc::hw_threads(), cfg.small ? "small" : "full", cfg.soft_s);
    bool ok = true;

#if defined(PC_MUTANT)
    // ---- mutation build: test only the broken counter -------------------------------------------
    ok = test_counter<mutant_counter>(mutant_counter::name, Speed::medium, cfg);
    std::printf("CHECKS_FIRED:%s%s%s%s%s%s\n", g_fired.final_value ? " final" : "",
                g_fired.duplicate ? " duplicate" : "", g_fired.missing ? " missing" : "",
                g_fired.order ? " order" : "", g_fired.realtime ? " real-time" : "",
                ok ? " none" : "");
    std::printf("MUTANT %s: %s\n", mutant_counter::name,
                ok ? "NOT CAUGHT by the value checks" : "caught by the value checks");
    return ok ? 0 : 1;
#else
    // ---- (c) fetch-and-add ----------------------------------------------------------------------
    ok &= test_counter<pc::counter_faa>("faa", Speed::fast, cfg);
    ok &= test_counter<pc::counter_faa_seqcst>("faa_seqcst", Speed::fast, cfg);
    // ---- (b) compare-and-swap -------------------------------------------------------------------
    ok &= test_counter<pc::counter_cas<pc::NoBackoff>>("cas", Speed::fast, cfg);
    ok &= test_counter<pc::counter_cas_seqcst>("cas_seqcst", Speed::fast, cfg);
    ok &= test_counter<pc::counter_cas<pc::SpinBackoff>>("cas_spin", Speed::fast, cfg);
    ok &= test_counter<pc::counter_cas<pc::YieldBackoff>>("cas_yield", Speed::fast, cfg);
    ok &= test_counter<pc::counter_cas<pc::SleepBackoff>>("cas_sleep", Speed::medium, cfg);
    // ---- (a) mutex lock: one counter per lock of Part 1 -----------------------------------------
#if defined(PC_FOR_EACH_LOCK)
    // The OpenMP lock is inside the OpenMP run-time (libgomp), which is not instrumented:
    // ThreadSanitizer does not see the ordering it creates and reports a FALSE data race on the
    // protected value. It is skipped in a TSan build; tsan_uninstrumented runs it anyway.
    bool tsan_skip_omp = false;
#if defined(PC_TSAN)
    tsan_skip_omp = !args.has("tsan_uninstrumented");
#endif
#define PC_TEST_LOCK_COUNTER(type, lock_name)                                                    \
    if (tsan_skip_omp && std::string(lock_name) == "omp_lock")                                   \
        std::printf("SKIP  %-22s (ThreadSanitizer: OpenMP run-time not instrumented, see docs/part1.md)\n", \
                    "lock_" lock_name);                                                          \
    else                                                                                         \
        ok &= test_counter<pc::counter_lock<type>>("lock_" lock_name, Speed::medium, cfg);
    PC_FOR_EACH_LOCK(PC_TEST_LOCK_COUNTER)
#undef PC_TEST_LOCK_COUNTER
#else
    std::printf("note: include/lock.h not found, the lock-based counter is tested with std::mutex only\n");
    ok &= test_counter<pc::counter_lock<std_mutex_adapter>>("lock_std_mutex", Speed::medium, cfg);
#endif
    std::printf("%s\n", ok ? "ALL COUNTER TESTS PASSED" : "SOME COUNTER TESTS FAILED");
    return ok ? 0 : 1;
#endif
}
