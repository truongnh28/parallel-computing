// sleep_latency.cpp -- how long do the waiting primitives used by the backoff policies really
// take?
//
// The backoff printed in the assignment calls std::this_thread::sleep_for(nanoseconds(delay))
// with a delay that starts at a few nanoseconds. sleep_for is a system call: the thread is taken
// off its core and put back by a kernel timer, so the real waiting time is decided by the
// operating system, not by `delay`. This program measures it, single-threaded:
//
//   sleep_for(d)   for d = 1 ns ... 1 ms          one sample per call
//   yield          std::this_thread::yield()      one sample per call
//   cpu_relax      pc::cpu_relax()                too short to time alone: a sample is the
//   isb / yield_insn (AArch64) or pause (x86)     average over a batch of 1000 calls
//   clock          two clock readings in a row    the resolution/overhead of the measurement
//
// Output: CSV, one row per primitive, all times in nanoseconds (min, median, 99th percentile,
// max over the samples). Runs in about one second.
    #include <algorithm>
#include <chrono>
#include <cstdio>
#include <thread>
#include <vector>

#include "platform.hpp"

namespace {

using Clock = std::chrono::steady_clock;

double ns_between(Clock::time_point a, Clock::time_point b) {
    return std::chrono::duration<double, std::nano>(b - a).count();
}

void print_stats(const char* what, long long requested_ns, std::vector<double>& samples) {
    std::sort(samples.begin(), samples.end());
    const std::size_t n = samples.size();
    std::printf("%s,%s,%s,%s,%lld,%zu,%.1f,%.1f,%.1f,%.1f\n", pc::host_name().c_str(), pc::os_name(),
                pc::arch_name(), what, requested_ns, n, samples.front(), samples[n / 2],
                samples[n * 99 / 100], samples.back());
    std::fflush(stdout);
}

// Times `calls` single executions of f.
template <class F>
void measure_each(const char* what, long long requested_ns, int calls, F f) {
    std::vector<double> samples(calls);
    for (int i = 0; i < calls; ++i) {
        const auto t0 = Clock::now();
        f();
        const auto t1 = Clock::now();
        samples[i] = ns_between(t0, t1);
    }
    print_stats(what, requested_ns, samples);
}

// For operations of a few nanoseconds: each sample is the mean over a batch of 1000 executions.
template <class F>
void measure_batched(const char* what, int batches, F f) {
    const int batch = 1000;
    std::vector<double> samples(batches);
    for (int i = 0; i < batches; ++i) {
        const auto t0 = Clock::now();
        for (int k = 0; k < batch; ++k) f();
        const auto t1 = Clock::now();
        samples[i] = ns_between(t0, t1) / batch;
    }
    print_stats(what, 0, samples);
}

}  // namespace

int main() {
    std::puts("host,os,arch,what,requested_ns,samples,min_ns,median_ns,p99_ns,max_ns");

    measure_each("clock", 0, 20000, [] {});

    const long long delays_ns[] = {1, 10, 100, 1000, 10000, 100000, 1000000};
    for (long long d : delays_ns) {
        // Fewer samples for the long sleeps so that each row takes a fraction of a second.
        const int calls = d >= 1000000 ? 200 : (d >= 100000 ? 1000 : 2000);
        measure_each("sleep_for", d, calls,
                     [d] { std::this_thread::sleep_for(std::chrono::nanoseconds(d)); });
    }

    measure_each("yield", 0, 20000, [] { std::this_thread::yield(); });

    measure_batched("cpu_relax", 2000, [] { pc::cpu_relax(); });
#if defined(__aarch64__)
    // Both AArch64 candidates for cpu_relax, measured side by side (see platform.hpp).
    measure_batched("isb", 2000, [] { asm volatile("isb" : : : "memory"); });
    measure_batched("yield_insn", 2000, [] { asm volatile("yield" : : : "memory"); });
#elif defined(__x86_64__) || defined(__i386__)
    measure_batched("pause", 2000, [] { asm volatile("pause" : : : "memory"); });
#endif
    return 0;
}
