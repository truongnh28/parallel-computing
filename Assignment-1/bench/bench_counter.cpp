// bench_counter.cpp -- the counter benchmark of the assignment (Part 3).
// Metric of the assignment: the execution time of threads x iters increments.
//
// Every thread runs
//
//     for (i = 0; i < iters; ++i) {
//         counter.increment();
//         for (j = 0; j < outside_work; ++j)
//             is_prime(n);                   // work between two increments (lowers contention)
//     }
//
// Usage:
//     bench_counter impl=<name> threads=<p> iters=<I per thread> outside_work=<OW> n=<prime>
//     bench_counter list        names of the available counters, one per line
//     bench_counter header      the CSV header
// Output: one CSV row (bench_common.hpp), part = "counter", ops = threads x iters. The measured
// time runs from the moment all threads are released together until the last one has finished.
//
// What keeps the optimiser from removing the work, and what the `ok` column checks:
//   * every value returned by increment() is added to a per-thread sum. The sum over all threads
//     must be 0 + 1 + ... + (M-1) with M = threads x iters. (A correct counter returns each of
//     these values exactly once. The test program checks that precisely; here the sum is a cheap
//     check that needs no memory.)
//   * the final value of the counter must be M;
//   * every is_prime result is added to a per-thread sum that must equal the number of calls,
//     and n goes through pc::opaque() at every call (same technique as in bench_lock.cpp).
//
// `extra` column: cas_fail:<failed CAS attempts of all threads> for the CAS counters;
// thr_min_s / thr_max_s: time of the fastest and of the slowest thread for its own loop.
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include "backoff.hpp"
#include "bench_common.hpp"
#include "counter.hpp"
#include "platform.hpp"
#include "workload.hpp"

// The locks of Part 1 (list macro PC_FOR_EACH_LOCK). Without lock.h only std::mutex is offered.
#if __has_include("lock.h")
#include "lock.h"
#else
#include <mutex>
#endif

namespace {

#if !defined(PC_FOR_EACH_LOCK)
struct std_mutex_adapter {
    std::mutex m;
    void acquire() { m.lock(); }
    void release() { m.unlock(); }
};
#endif

struct Config {
    std::string impl;
    int threads = 0;
    long long iters = 0;
    long long outside_work = 0;
    int n = 0;
};

// Private results of one thread, on its own cache line (written once, at the end of the loop).
struct alignas(pc::kCacheLine) PerThread {
    std::uint64_t value_sum = 0;     // sum of the values returned by increment()
    long long prime_sum = 0;         // sum of the is_prime results
    std::uint64_t cas_failed = 0;    // failed CAS attempts (0 for non-CAS counters)
    double seconds = 0;              // time this thread needed for its loop
};

// Failed CAS attempts of the calling thread; 0 for the counters that have no such statistic.
// `requires` asks at compile time whether Counter has a static member failed_attempts().
template <class Counter>
std::uint64_t failed_attempts_of_this_thread() {
    if constexpr (requires { Counter::failed_attempts(); }) return Counter::failed_attempts();
    else return 0;
}

template <class Counter>
constexpr bool has_cas_statistic = requires { Counter::failed_attempts(); };

template <class Counter>
pc::Row run(const Config& c) {
    Counter counter;                           // the shared counter (aligned to a cache line)
    std::vector<PerThread> per(static_cast<std::size_t>(c.threads));

    const double seconds = pc::timed_run(c.threads, [&](int tid) {
        const long long iters = c.iters;       // copies: the loop bounds stay in registers
        const long long outside_work = c.outside_work;
        const int n = c.n;
        Counter& shared = counter;             // same for the address of the counter
        std::uint64_t value_sum = 0;
        long long prime_sum = 0;
        const double t0 = pc::now_s();
        for (long long i = 0; i < iters; ++i) {
            value_sum += static_cast<std::uint64_t>(shared.increment());    // the result is used
            for (long long j = 0; j < outside_work; ++j)
                prime_sum += pc::is_prime(pc::opaque(n));
        }
        PerThread& mine = per[static_cast<std::size_t>(tid)];
        mine.seconds = pc::now_s() - t0;
        mine.value_sum = value_sum;
        mine.prime_sum = prime_sum;
        // The thread is new, so its thread_local failure count started at 0.
        mine.cas_failed = failed_attempts_of_this_thread<Counter>();
    });

    // ---- result checks ----
    // Unsigned 64-bit arithmetic wraps around in the same way on both sides of the comparison,
    // so the check stays valid even if M(M-1)/2 does not fit in 64 bits.
    const std::uint64_t m = static_cast<std::uint64_t>(c.threads) * static_cast<std::uint64_t>(c.iters);
    const std::uint64_t expected_sum = (m % 2 == 0) ? (m / 2) * (m - 1) : m * ((m - 1) / 2);
    const long long expected_primes = c.iters * c.outside_work;

    std::uint64_t value_sum = 0, cas_failed = 0;
    bool ok = true;
    double fastest = per[0].seconds, slowest = per[0].seconds;
    for (const PerThread& p : per) {
        value_sum += p.value_sum;
        cas_failed += p.cas_failed;
        if (p.prime_sum != expected_primes) ok = false;
        fastest = std::min(fastest, p.seconds);
        slowest = std::max(slowest, p.seconds);
    }
    if (value_sum != expected_sum) ok = false;
    if (static_cast<std::uint64_t>(counter.get()) != m) ok = false;

    pc::Row r;
    r.part = "counter";
    r.impl = c.impl;
    r.threads = c.threads;
    r.iters = c.iters;
    r.outside_work = c.outside_work;
    r.n = c.n;
    r.seconds = seconds;
    r.ops = static_cast<double>(m);            // total number of increments
    r.ok = ok;
    char extra[160];
    if (has_cas_statistic<Counter>)
        std::snprintf(extra, sizeof extra, "cas_fail:%llu;thr_min_s:%.6f;thr_max_s:%.6f",
                      static_cast<unsigned long long>(cas_failed), fastest, slowest);
    else
        std::snprintf(extra, sizeof extra, "thr_min_s:%.6f;thr_max_s:%.6f", fastest, slowest);
    r.extra = extra;
    return r;
}

}  // namespace

// The list of all counters as (type, name) pairs, in the same style as PC_FOR_EACH_LOCK.
// Each X(type, name) becomes its own instantiation of run<type>(): no virtual call in the loop.
#define PC_COUNTERS_ATOMIC(X)                                   \
    X(pc::counter_faa, "faa")                                   \
    X(pc::counter_faa_seqcst, "faa_seqcst")                     \
    X(pc::counter_cas<pc::NoBackoff>, "cas")                    \
    X(pc::counter_cas_seqcst, "cas_seqcst")                     \
    X(pc::counter_cas<pc::SpinBackoff>, "cas_spin")             \
    X(pc::counter_cas<pc::YieldBackoff>, "cas_yield")           \
    X(pc::counter_cas<pc::SleepBackoff>, "cas_sleep")

// One lock-based counter per lock of Part 1: "lock_" + the name of the lock.
#if defined(PC_FOR_EACH_LOCK)
#define PC_COUNTER_OF_LOCK(lock_type, lock_name) \
    PC_X(pc::counter_lock<lock_type>, "lock_" lock_name)
#define PC_COUNTERS_LOCK PC_FOR_EACH_LOCK(PC_COUNTER_OF_LOCK)
#else
#define PC_COUNTERS_LOCK PC_X(pc::counter_lock<std_mutex_adapter>, "lock_std_mutex")
#endif

int main(int argc, char** argv) {
    const pc::Args args(argc, argv);

    if (args.has("list")) {
#define PC_X(Type, name) std::puts(name);
        PC_COUNTERS_ATOMIC(PC_X)
        PC_COUNTERS_LOCK
#undef PC_X
        return 0;
    }
    if (args.has("header")) {
        std::puts(pc::csv_header());
        return 0;
    }

    Config c;
    c.impl = args.str("impl", "faa");
    c.threads = static_cast<int>(args.num("threads", 4));
    c.iters = args.num("iters", 1000000);
    c.outside_work = args.num("outside_work", 0);
    c.n = static_cast<int>(args.num("n", 101));

    if (c.threads < 1 || c.iters < 1 || c.outside_work < 0) {
        std::fprintf(stderr, "bench_counter: need threads >= 1, iters >= 1, outside_work >= 0\n");
        return 2;
    }
    if (!pc::is_prime_checked(c.n)) {
        std::fprintf(stderr, "bench_counter: n=%d is not prime (is_prime would stop at the first divisor)\n", c.n);
        return 2;
    }

    // The name selects which instantiation of run<> is called.
#define PC_X(Type, name)                \
    if (c.impl == name) {               \
        pc::print_row(run<Type>(c));    \
        return 0;                       \
    }
    PC_COUNTERS_ATOMIC(PC_X)
    PC_COUNTERS_LOCK
#undef PC_X

    std::fprintf(stderr, "bench_counter: unknown impl '%s' (run `bench_counter list`)\n", c.impl.c_str());
    return 2;
}
