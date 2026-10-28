// test_locks.cpp -- correctness tests for every lock in lock.h (the assignment's locks and the
// library baselines go through exactly the same templated tests).
//
// What is checked, per lock:
//   1. Mutual exclusion, fixed number of iterations. Inside the critical section the threads
//      increment a NON-atomic counter (read, add one, write). If two threads are ever inside
//      together, an update is lost and the final value is below threads x iters. In addition an
//      occupancy counter records how many threads are inside; a thread that finds somebody
//      already there has directly observed a violation.
//   2. The same check for a fixed TIME instead of a fixed count, with per-thread acquisition
//      counts. It is run with few threads, with one thread per hardware thread, and with twice
//      as many threads as hardware threads (oversubscription: owners and waiters get preempted).
//      The counts also give the fairness of the lock: max/min acquisitions over the threads.
//      Fairness is reported, not judged (an unfair lock is still a correct lock).
//   3. No deadlock: a watchdog aborts the program with a message if one sub-test does not
//      finish before its deadline.
//
// Exit status: 0 = all locks passed, 1 = a mutual exclusion check failed, 2 = watchdog (deadlock).
//
// Usage: test_locks [small] [only=<lock>] [skip=<lock>] [watchdog=<seconds>] [ms=<milliseconds>]
//   small     10x fewer iterations and shorter timed runs (used for the ThreadSanitizer build)
//   only=...  test a single lock (used by the mutation tests)
//   skip=...  leave one lock out; the omission is printed (for a library lock that
//             ThreadSanitizer cannot analyse, e.g. an OpenMP runtime that is not instrumented)
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "bench_common.hpp"
#include "lock.h"
#include "platform.hpp"
#include "workload.hpp"

namespace {

// ---- watchdog -----------------------------------------------------------------------------------
// One background thread. A sub-test arms it with a description and disarms it when it has
// finished. If the sub-test is still running `seconds` later, the lock under test has deadlocked
// (or lost a hand-over): the stuck threads cannot be joined, so the whole process is terminated.
class Watchdog {
  public:
    explicit Watchdog(double seconds) : seconds_(seconds), thread_([this] { run(); }) {}

    ~Watchdog() {
        {
            std::lock_guard<std::mutex> g(m_);
            quit_ = true;
        }
        thread_.join();
    }

    void arm(const std::string& what) {
        std::lock_guard<std::mutex> g(m_);
        what_ = what;
        deadline_ = pc::now_s() + seconds_;
    }

    void disarm() {
        std::lock_guard<std::mutex> g(m_);
        deadline_ = 0;
    }

  private:
    void run() {
        for (;;) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            std::lock_guard<std::mutex> g(m_);
            if (quit_) return;
            if (deadline_ > 0 && pc::now_s() > deadline_) {
                std::printf("\nFAIL  %s: DEADLOCK -- not finished after %.0f s (watchdog)\n",
                            what_.c_str(), seconds_);
                std::fflush(stdout);
                std::_Exit(2);      // no clean-up: the worker threads are stuck inside acquire()
            }
        }
    }

    std::mutex m_;                  // protects the three fields below
    std::string what_;              // description of the running sub-test
    double deadline_ = 0;           // absolute time in seconds; 0 = not armed
    bool quit_ = false;
    const double seconds_;
    std::thread thread_;            // declared last: it starts when all other members exist
};

// ---- the critical section used by all tests -----------------------------------------------------
struct alignas(pc::kCacheLine) Shared {
    long long counter = 0;                    // NOT atomic: protected only by the lock under test
    std::atomic<int> inside{0};               // number of threads inside the critical section
    std::atomic<long long> violations{0};     // times a thread entered while another was inside
};

// relaxed on all three atomic operations: they are test instrumentation and must not order
// anything. With a stronger order they would themselves synchronise the threads, and
// ThreadSanitizer would then no longer see that a broken lock fails to do so.
inline void critical_section(Shared& s) {
    if (s.inside.fetch_add(1, std::memory_order_relaxed) != 0)
        s.violations.fetch_add(1, std::memory_order_relaxed);
    long long c = s.counter;                  // read
    s.counter = pc::opaque(c) + 1;            // add one, write back (opaque: keep the two apart)
    s.inside.fetch_sub(1, std::memory_order_relaxed);
}

struct alignas(pc::kCacheLine) PerThread {
    long long acquired = 0;                   // written by its thread, read by main after join
};

struct Outcome {
    bool ok = false;
    long long expected = 0;                   // number of critical sections that were executed
    long long counter = 0;                    // final value of the non-atomic counter
    long long violations = 0;
    long long min_acquired = 0, max_acquired = 0;   // per-thread acquisition counts (timed test)
};

// ---- test 1: fixed number of iterations ---------------------------------------------------------
template <class Lock>
Outcome run_fixed_iters(int threads, long long iters) {
    Lock lock;
    Shared shared;
    pc::timed_run(threads, [&](int) {
        for (long long i = 0; i < iters; ++i) {
            lock.acquire();
            critical_section(shared);
            lock.release();
        }
    });
    Outcome o;
    o.expected = threads * iters;
    o.counter = shared.counter;
    o.violations = shared.violations.load();
    o.ok = (o.counter == o.expected) && (o.violations == 0);
    return o;
}

// ---- test 2: fixed time, per-thread acquisition counts ------------------------------------------
template <class Lock>
Outcome run_fixed_time(int threads, int milliseconds) {
    Lock lock;
    Shared shared;
    std::vector<PerThread> per(threads);
    std::atomic<bool> stop{false};
    std::thread timer([&] {
        std::this_thread::sleep_for(std::chrono::milliseconds(milliseconds));
        stop.store(true, std::memory_order_relaxed);
    });
    pc::timed_run(threads, [&](int tid) {
        long long n = 0;
        // relaxed: `stop` only tells the thread to leave the loop; no data depends on it.
        while (!stop.load(std::memory_order_relaxed)) {
            lock.acquire();
            critical_section(shared);
            lock.release();
            ++n;
        }
        per[tid].acquired = n;
    });
    timer.join();

    Outcome o;
    o.min_acquired = o.max_acquired = per[0].acquired;
    for (const PerThread& p : per) {
        o.expected += p.acquired;
        o.min_acquired = std::min(o.min_acquired, p.acquired);
        o.max_acquired = std::max(o.max_acquired, p.acquired);
    }
    o.counter = shared.counter;
    o.violations = shared.violations.load();
    o.ok = (o.counter == o.expected) && (o.violations == 0);
    return o;
}

// ---- driver --------------------------------------------------------------------------------------
struct Config {
    bool small = false;
    std::string only;
    std::string skip;
    double watchdog_s = 20;
    int ms = 300;
    int hw = 1;
};

void report_failure(const char* lock, const std::string& what, const Outcome& o) {
    std::printf("\nFAIL  %s %s: counter=%lld expected=%lld (lost %lld), threads seen inside together=%lld\n",
                lock, what.c_str(), o.counter, o.expected, o.expected - o.counter, o.violations);
}

// "max/min" acquisitions as text; a thread that never got the lock gives "inf".
std::string ratio_text(const Outcome& o) {
    if (o.min_acquired == 0) return "inf";
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.2f", double(o.max_acquired) / double(o.min_acquired));
    return buf;
}

// Runs all sub-tests for one lock type and prints one PASS/FAIL line. Returns true on PASS.
template <class Lock>
bool test_lock(const char* name, const Config& cfg, Watchdog& watchdog) {
    bool ok = true;
    std::string line;

    // Test 1: fixed iterations with 1 thread, 2 threads, and one thread per hardware thread.
    const long long scale = cfg.small ? 10 : 1;
    const int thread_counts[3] = {1, 2, cfg.hw};
    const long long iter_counts[3] = {200000 / scale, 100000 / scale, 20000 / scale};
    for (int k = 0; k < 3; ++k) {
        const std::string what = "fixed-iters threads=" + std::to_string(thread_counts[k]) +
                                 " iters=" + std::to_string(iter_counts[k]);
        watchdog.arm(std::string(name) + " " + what);
        const Outcome o = run_fixed_iters<Lock>(thread_counts[k], iter_counts[k]);
        watchdog.disarm();
        if (!o.ok) { ok = false; report_failure(name, what, o); }
    }

    // Test 2: fixed time with few threads, all hardware threads, and 2x oversubscription.
    const int timed_threads[3] = {std::min(4, cfg.hw), cfg.hw, 2 * cfg.hw};
    for (int k = 0; k < 3; ++k) {
        const std::string what = "fixed-time threads=" + std::to_string(timed_threads[k]) +
                                 " ms=" + std::to_string(cfg.ms);
        watchdog.arm(std::string(name) + " " + what);
        const Outcome o = run_fixed_time<Lock>(timed_threads[k], cfg.ms);
        watchdog.disarm();
        if (!o.ok) { ok = false; report_failure(name, what, o); }
        char buf[96];
        std::snprintf(buf, sizeof buf, "  %2dt: %9lld acq, max/min %-7s", timed_threads[k],
                      o.expected, ratio_text(o).c_str());
        line += buf;
    }

    std::printf("%s  %-16s%s\n", ok ? "PASS" : "FAIL", name, line.c_str());
    std::fflush(stdout);
    return ok;
}

}  // namespace

int main(int argc, char** argv) {
    pc::Args args(argc, argv);
    Config cfg;
    cfg.small = args.has("small");
    cfg.only = args.str("only", "");
    cfg.skip = args.str("skip", "");
    cfg.watchdog_s = double(args.num("watchdog", 20));
    cfg.ms = int(args.num("ms", cfg.small ? 100 : 300));
    cfg.hw = int(pc::hw_threads());

    Watchdog watchdog(cfg.watchdog_s);

    std::printf("# test_locks on %s (%s, %s, %s), %d hardware threads%s\n", pc::host_name().c_str(),
                pc::arch_name(), pc::os_name(), pc::compiler_name().c_str(), cfg.hw,
                cfg.small ? ", small sizes" : "");
    std::printf("# per lock: mutual exclusion with fixed iterations (1, 2, %d threads), then %d ms timed runs;\n"
                "# columns show total acquisitions and the max/min per-thread ratio (fairness; 1.00 = even)\n",
                cfg.hw, cfg.ms);

    int tested = 0, failed = 0;
#define X(Type, name)                                               \
    if (cfg.skip == name) {                                         \
        std::printf("SKIP  %s (skip= argument)\n", name);           \
    } else if (cfg.only.empty() || cfg.only == name) {              \
        ++tested;                                                   \
        if (!test_lock<Type>(name, cfg, watchdog)) ++failed;        \
    }
    PC_FOR_EACH_LOCK(X)
#undef X

    if (tested == 0) {
        std::fprintf(stderr, "test_locks: no lock named '%s'\n", cfg.only.c_str());
        return 64;
    }
    std::printf("# %d lock(s) tested, %d failed\n", tested, failed);
    return failed == 0 ? 0 : 1;
}
