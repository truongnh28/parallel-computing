// handoff_gap.cpp -- extra experiment for the report (RQ2): why does MCS collapse at the core count?
//
// Hypothesis: MCS hands the lock to one fixed successor (FIFO). When that successor has no core,
// the whole queue loses a scheduling slice, so a small fraction of hand-overs becomes very long.
// TAS/TTAS do not suffer from this because any running thread can take a free lock, and library
// locks put waiters to sleep in the kernel.
//
// The loop is the one of bench_lock.cpp (N = 1009, OUTSIDE_WORK = 0). In addition, the thread that
// releases the lock stores a timestamp just before release(), and the thread that acquires it next
// measures the gap "previous release -> my acquire". Only the lock holder writes the timestamp,
// so a relaxed atomic is enough. The program also reports the process's voluntary and involuntary
// context switches (getrusage) and the 1-minute load average at the start of the run.
//
// Usage: handoff_gap lock=<name> threads=<p> iters=<I per thread> [n=1009] [big_us=100]
// Output: one CSV row, header with `handoff_gap header`.
#include <sys/resource.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "bench_common.hpp"
#include "lock.h"
#include "platform.hpp"
#include "workload.hpp"

namespace {

using clk = std::chrono::steady_clock;

inline long long now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(clk::now().time_since_epoch()).count();
}

struct alignas(pc::kCacheLine) PerThread {
    long long sum = 0;
    long long gaps = 0;           // hand-overs observed by this thread
    long long big = 0;            // hand-overs longer than big_ns
    long long big_ns = 0;         // total length of those long hand-overs
    long long all_ns = 0;         // total length of all hand-overs
    long long max_ns = 0;
};

struct Config {
    std::string lock;
    int threads = 4;
    long long iters = 10000;
    int n = 1009;
    long long big_ns = 100000;
};

template <class Lock>
int run(const Config& c) {
    Lock lock;
    alignas(pc::kCacheLine) std::atomic<long long> last_release{0};
    std::vector<PerThread> per(c.threads);

    rusage r0{}, r1{};
    getrusage(RUSAGE_SELF, &r0);
    double load[1] = {0};
    getloadavg(load, 1);

    const double seconds = pc::timed_run(c.threads, [&](int tid) {
        PerThread me;
        for (long long i = 0; i < c.iters; ++i) {
            lock.acquire();
            const long long t_acq = now_ns();
            const long long t_rel = last_release.load(std::memory_order_relaxed);
            if (t_rel != 0) {
                const long long g = t_acq - t_rel;
                ++me.gaps;
                me.all_ns += g;
                me.max_ns = std::max(me.max_ns, g);
                if (g > c.big_ns) {
                    ++me.big;
                    me.big_ns += g;
                }
            }
            me.sum += pc::is_prime(pc::opaque(c.n));
            last_release.store(now_ns(), std::memory_order_relaxed);
            lock.release();
        }
        per[tid] = me;
    });
    getrusage(RUSAGE_SELF, &r1);

    PerThread tot;
    bool ok = true;
    for (const PerThread& p : per) {
        if (p.sum != c.iters) ok = false;
        tot.gaps += p.gaps;
        tot.big += p.big;
        tot.big_ns += p.big_ns;
        tot.all_ns += p.all_ns;
        tot.max_ns = std::max(tot.max_ns, p.max_ns);
    }
    const double cs = double(c.threads) * double(c.iters);
    std::printf("%s,%d,%lld,%d,%.6f,%.1f,%lld,%lld,%.6f,%.1f,%.6f,%.1f,%.1f,%ld,%ld,%.2f,%d\n",
                c.lock.c_str(), c.threads, c.iters, c.n, seconds, 1e9 * seconds / cs, tot.gaps, tot.big,
                tot.gaps ? double(tot.big) / double(tot.gaps) : 0.0,
                tot.big ? 1e-3 * double(tot.big_ns) / double(tot.big) : 0.0,  // mean long gap, us
                1e-9 * double(tot.big_ns),                                     // time in long gaps, s
                tot.gaps ? double(tot.all_ns) / double(tot.gaps) : 0.0,        // mean gap, ns
                1e-3 * double(tot.max_ns),                                     // max gap, us
                long(r1.ru_nvcsw - r0.ru_nvcsw), long(r1.ru_nivcsw - r0.ru_nivcsw), load[0], ok ? 1 : 0);
    return ok ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
    pc::Args args(argc, argv);
    if (args.has("header")) {
        std::puts("lock,threads,iters,n,seconds,ns_per_cs,handovers,long_handovers,long_fraction,"
                  "mean_long_us,time_in_long_s,mean_gap_ns,max_gap_us,nvcsw,nivcsw,load1,ok");
        return 0;
    }
    Config c;
    c.lock = args.str("lock", "mcs");
    c.threads = int(args.num("threads", 4));
    c.iters = args.num("iters", 10000);
    c.n = int(args.num("n", 1009));
    c.big_ns = 1000 * args.num("big_us", 100);
#define X(Type, name) \
    if (c.lock == name) return run<Type>(c);
    PC_FOR_EACH_LOCK(X)
#undef X
    std::fprintf(stderr, "handoff_gap: unknown lock '%s'\n", c.lock.c_str());
    return 2;
}
