// bench_barrier.cpp -- Part 2 benchmark: barrier episodes per unit of time.
//
// The assignment's metric: the number of completed barrier episodes per unit of time, with NO
// work between two barriers. Every thread runs
//
//     for (i = 0; i < iters; ++i) barrier.wait(tid);
//
// so one run performs exactly `iters` episodes (an episode is one collective crossing of the
// barrier by all threads, not one call of wait()). The program prints one CSV row; the column
// ops is the number of episodes, so ops_per_s is the metric.
//
// Usage:
//   bench_barrier impl=<name> threads=<p> iters=<episodes>
//   bench_barrier list        names of the implementations compiled into this binary
//   bench_barrier header      the CSV header
#include <cstdio>
#include <string>
#include <vector>

#ifdef _OPENMP
#include <omp.h>
#endif

#include "barrier.hpp"
#include "bench_common.hpp"

namespace {

// Number of episodes a thread has completed, written once at the end of its loop.
// One cache line per thread so that these writes do not disturb the other threads.
struct alignas(pc::kCacheLine) Done {
    long long episodes = 0;
};

// Builds the CSV row. ok = every thread completed all `iters` episodes.
pc::Row make_row(const char* impl, int threads, long long iters, double seconds,
                 const std::vector<Done>& done, const char* extra) {
    bool ok = true;
    for (const Done& d : done)
        if (d.episodes != iters) ok = false;

    pc::Row row;
    row.part = "barrier";
    row.impl = impl;
    row.threads = threads;
    row.iters = iters;
    row.seconds = seconds;
    row.ops = static_cast<double>(iters);       // episodes, NOT threads * iters
    row.ok = ok;
    row.extra = extra;
    return row;
}

// The measurement for a barrier whose participants are ordinary threads.
// pc::timed_run starts the clock when all threads are released together and stops it when the
// last one has finished, so thread creation is not measured.
template <class B>
pc::Row run(const char* impl, int threads, long long iters, const char* extra) {
    B barrier(threads);
    std::vector<Done> done(static_cast<std::size_t>(threads));
    const double seconds = pc::timed_run(threads, [&](int tid) {
        long long completed = 0;
        for (long long i = 0; i < iters; ++i) {
            barrier.wait(tid);                  // nothing else between two barriers
            ++completed;
        }
        done[static_cast<std::size_t>(tid)].episodes = completed;
    });
    return make_row(impl, threads, iters, seconds, done, extra);
}

#ifdef PC_HAVE_OMP_BARRIER
// The measurement for the OpenMP barrier. "#pragma omp barrier" only synchronises the threads
// of an OpenMP team, so the threads must be created by OpenMP and pc::timed_run cannot be used.
// The measured interval is the same in spirit: the clock starts when the whole team exists and
// stops when the parallel region has ended. It contains two extra implicit barriers (end of
// "single", end of the region), which is negligible against `iters` episodes.
pc::Row run_openmp(const char* impl, int threads, long long iters, const char* extra) {
    pc::OmpBarrier barrier(threads);
    std::vector<Done> done(static_cast<std::size_t>(threads));
    omp_set_dynamic(0);                         // exactly `threads` threads, not "up to"
    int team = 0;
    double start = 0.0;
    #pragma omp parallel num_threads(threads)
    {
        const int tid = omp_get_thread_num();
        #pragma omp single
        {
            team = omp_get_num_threads();
            start = pc::now_s();
        }                                       // implicit barrier: all threads continue together
        long long completed = 0;
        if (team == threads) {
            for (long long i = 0; i < iters; ++i) {
                barrier.wait(tid);
                ++completed;
            }
        }
        done[static_cast<std::size_t>(tid)].episodes = completed;
    }
    const double seconds = pc::now_s() - start;
    return make_row(impl, threads, iters, seconds, done, extra);   // ok = false if the team was smaller
}
#endif

// All implementations, in the order printed by `list`.
struct Impl {
    const char* name;
    pc::Row (*run)(const char* impl, int threads, long long iters, const char* extra);
    std::string extra;          // goes into the CSV column "extra"
};

std::vector<Impl> implementations() {
    // The backoff bounds are compile-time constants (backoff.hpp); they are recorded with the
    // measurement so that a row can always be traced back to its configuration.
    const std::string sleep_bounds = "sleep_min_ns:" + std::to_string(PC_SLEEP_MIN_NS) +
                                     ";sleep_max_ns:" + std::to_string(PC_SLEEP_MAX_NS);
    const std::string spin_bounds = "spin_min:" + std::to_string(PC_SPIN_MIN) +
                                    ";spin_max:" + std::to_string(PC_SPIN_MAX);
    return {
        // sense-reversing barrier, five ways of waiting (names as for the locks of Part 1)
        {"sr",          run<pc::SenseBarrier<pc::NoBackoff>>,    ""},            // pure spinning
        {"sr_bo_spin",  run<pc::SenseBarrier<pc::SpinBackoff>>,  spin_bounds},   // exponential backoff, spinning
        {"sr_bo_yield", run<pc::SenseBarrier<pc::YieldBackoff>>, ""},            // spin 64 times, then yield
        {"sr_bo_sleep", run<pc::SenseBarrier<pc::SleepBackoff>>, sleep_bounds},  // the assignment's backoff (sleep)
        {"sr_block",    run<pc::SenseBarrierBlocking>,           ""},            // std::atomic::wait / notify_all
        // existing barriers
        {"std_barrier", run<pc::StdBarrier>,     ""},
        {"condvar",     run<pc::CondVarBarrier>, ""},
#ifdef PC_HAVE_PTHREAD_BARRIER
        {"pthread_barrier", run<pc::PthreadBarrier>, ""},
#endif
#ifdef PC_HAVE_OMP_BARRIER
        {"omp_barrier", run_openmp, ""},
#endif
    };
}

}  // namespace

int main(int argc, char** argv) {
    pc::Args args(argc, argv);
    const std::vector<Impl> impls = implementations();
    if (args.has("list")) {
        for (const Impl& impl : impls) std::printf("%s\n", impl.name);
        return 0;
    }
    if (args.has("header")) {
        std::printf("%s\n", pc::csv_header());
        return 0;
    }

    const std::string name = args.str("impl", "sr");
    const int threads = static_cast<int>(args.num("threads", 4));
    const long long iters = args.num("iters", 100000);
    if (threads < 1 || iters < 1) {
        std::fprintf(stderr, "bench_barrier: threads and iters must be >= 1\n");
        return 2;
    }

    for (const Impl& impl : impls) {
        if (name == impl.name) {
            const pc::Row row = impl.run(impl.name, threads, iters, impl.extra.c_str());
            pc::print_row(row);
            return row.ok ? 0 : 1;
        }
    }
    std::fprintf(stderr, "bench_barrier: unknown impl '%s' (run 'bench_barrier list')\n", name.c_str());
    return 2;
}
