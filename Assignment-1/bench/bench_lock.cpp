// bench_lock.cpp -- the lock benchmark of the assignment.
//
// Every thread runs the loop of the assignment's skeleton:
//
//     for (i = 0; i < NUM_OF_ITERS; ++i) {
//         lock.acquire();
//         is_prime(N);                       // work inside the critical section
//         lock.release();
//         for (j = 0; j < OUTSIDE_WORK; ++j)
//             is_prime(N);                   // work outside the critical section
//     }
//
// Differences from the skeleton, and why:
//   * The four parameters and the lock are chosen at run time (key=value arguments) instead of
//     with #define, so a parameter sweep needs no recompilation. Each lock type still has its own
//     instantiation of run<Lock>(), so the hot loop contains no virtual call.
//   * The result of every is_prime call is added to a per-thread sum, and N goes through
//     pc::opaque() at every call. Otherwise the compiler may delete the calls (their result is
//     unused) or compute is_prime(N) once and reuse it. At the end each sum must equal the number
//     of calls, iters * (1 + outside_work); this is the `ok` column.
//   * N must be prime, otherwise is_prime returns at the first divisor and does almost no work.
//
// Usage:
//     bench_lock lock=<name> threads=<p> iters=<I> outside_work=<OW> n=<prime>
//     bench_lock list        names of the available locks, one per line
//     bench_lock header      the CSV header
// Output: one CSV row (see bench_common.hpp). The measured time runs from the moment all threads
// are released together until the last one has finished its loop.
#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>

#include "bench_common.hpp"
#include "lock.h"
#include "platform.hpp"
#include "workload.hpp"

namespace {

struct Config {
    std::string lock;
    int threads = 0;
    long long iters = 0;
    long long outside_work = 0;
    int n = 0;
};

// Private results of one thread, on its own cache line.
struct alignas(pc::kCacheLine) PerThread {
    long long sum = 0;        // sum of all is_prime results of this thread
    double seconds = 0;       // time this thread needed for its loop
};

// Which instruction pc::cpu_relax() is in this build (recorded in the CSV row).
const char* relax_name() {
#if defined(__x86_64__) || defined(__i386__)
    return "pause";
#elif defined(__aarch64__) && defined(PC_RELAX_YIELD)
    return "yield";
#elif defined(__aarch64__)
    return "isb";
#else
    return "none";
#endif
}

template <class Lock>
pc::Row run(const Config& c) {
    Lock lock;                                 // the shared lock (aligned to a cache line)
    std::vector<PerThread> per(c.threads);

    const double seconds = pc::timed_run(c.threads, [&](int tid) {
        const long long iters = c.iters;       // copies: the loop bounds stay in registers
        const long long outside_work = c.outside_work;
        const int n = c.n;
        long long sum = 0;
        const double t0 = pc::now_s();
        for (long long i = 0; i < iters; ++i) {
            lock.acquire();
            sum += pc::is_prime(pc::opaque(n));
            lock.release();
            for (long long j = 0; j < outside_work; ++j)
                sum += pc::is_prime(pc::opaque(n));
        }
        per[tid].seconds = pc::now_s() - t0;
        per[tid].sum = sum;
    });

    // Every call must have returned 1 (N is prime), so each sum equals the number of calls.
    const long long expected = c.iters * (1 + c.outside_work);
    bool ok = true;
    double fastest = per[0].seconds, slowest = per[0].seconds;
    for (const PerThread& p : per) {
        if (p.sum != expected) ok = false;
        fastest = std::min(fastest, p.seconds);
        slowest = std::max(slowest, p.seconds);
    }

    pc::Row r;
    r.part = "lock";
    r.impl = c.lock;
    r.threads = c.threads;
    r.iters = c.iters;
    r.outside_work = c.outside_work;
    r.n = c.n;
    r.seconds = seconds;
    r.ops = double(c.threads) * double(c.iters);       // number of critical sections executed
    r.ok = ok;
    // All threads do the same number of acquisitions, so unfairness shows up as time: with an
    // unfair lock some threads finish their loop much earlier than others.
    char extra[128];
    std::snprintf(extra, sizeof extra, "thr_min_s:%.6f;thr_max_s:%.6f;relax:%s", fastest, slowest,
                  relax_name());
    r.extra = extra;
    return r;
}

}  // namespace

int main(int argc, char** argv) {
    pc::Args args(argc, argv);

    if (args.has("list")) {
#define X(Type, name) std::puts(name);
        PC_FOR_EACH_LOCK(X)
#undef X
        return 0;
    }
    if (args.has("header")) {
        std::puts(pc::csv_header());
        return 0;
    }

    Config c;
    c.lock = args.str("lock", "tas");
    c.threads = int(args.num("threads", 4));
    c.iters = args.num("iters", 100000);
    c.outside_work = args.num("outside_work", 0);
    c.n = int(args.num("n", 1009));

    if (c.threads < 1 || c.iters < 1 || c.outside_work < 0) {
        std::fprintf(stderr, "bench_lock: need threads >= 1, iters >= 1, outside_work >= 0\n");
        return 2;
    }
    if (!pc::is_prime_checked(c.n)) {
        std::fprintf(stderr, "bench_lock: n=%d is not prime (is_prime would stop at the first divisor)\n", c.n);
        return 2;
    }

    // One instantiation of run<> per lock type; the name selects which one is called.
#define X(Type, name)                   \
    if (c.lock == name) {               \
        pc::print_row(run<Type>(c));    \
        return 0;                       \
    }
    PC_FOR_EACH_LOCK(X)
#undef X

    std::fprintf(stderr, "bench_lock: unknown lock '%s' (run `bench_lock list`)\n", c.lock.c_str());
    return 2;
}
