// bench_common.hpp -- pieces shared by the three benchmark programs and by the tests:
// command-line parsing ("key=value"), a start gate so that all threads begin together, and the
// CSV row format. Every program prints exactly one CSV row per run; scripts/run_bench.py collects
// the rows. Nothing is ever copied from the terminal by hand.
#pragma once

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <map>
#include <string>
#include <thread>
#include <vector>

#include "platform.hpp"

namespace pc {

// ---- command line: prog key=value key=value ... ------------------------------------------------
class Args {
    std::map<std::string, std::string> kv_;
  public:
    Args(int argc, char** argv) {
        for (int i = 1; i < argc; ++i) {
            std::string a(argv[i]);
            auto eq = a.find('=');
            if (eq == std::string::npos) kv_[a] = std::string("1");   // explicit string: g++ 12 warns on the literal
            else kv_[a.substr(0, eq)] = a.substr(eq + 1);
        }
    }
    bool has(const std::string& k) const { return kv_.count(k) != 0; }
    std::string str(const std::string& k, const std::string& def) const {
        auto it = kv_.find(k);
        return it == kv_.end() ? def : it->second;
    }
    long long num(const std::string& k, long long def) const {
        auto it = kv_.find(k);
        return it == kv_.end() ? def : std::atoll(it->second.c_str());
    }
};

// ---- timed parallel region ---------------------------------------------------------------------
// Starts `threads` threads running body(tid). All threads wait at a start gate; the clock starts
// when the gate opens and stops when the last thread has finished its body. Thread creation and
// join are therefore outside the measured interval (the report states this choice).
// Returns the elapsed time in seconds.
inline double timed_run(int threads, const std::function<void(int)>& body) {
    std::atomic<int> ready{0};
    std::atomic<bool> go{false};
    std::atomic<int> done{0};
    std::vector<std::thread> th;
    th.reserve(threads);
    for (int t = 0; t < threads; ++t) {
        th.emplace_back([&, t] {
            ready.fetch_add(1, std::memory_order_release);
            while (!go.load(std::memory_order_acquire)) cpu_relax();
            body(t);
            done.fetch_add(1, std::memory_order_release);
        });
    }
    while (ready.load(std::memory_order_acquire) != threads) std::this_thread::yield();
    const double t0 = now_s();
    go.store(true, std::memory_order_release);
    // The main thread sleeps in join(); the end time is taken after the last join returns.
    for (auto& x : th) x.join();
    return now_s() - t0;
}

// ---- CSV ---------------------------------------------------------------------------------------
// Columns common to all parts. `extra` is a free "k:v;k:v" field for part-specific counters
// (CAS failures, per-thread acquisition spread, ...).
inline const char* csv_header() {
    return "host,arch,os,compiler,ncpu,part,impl,threads,iters,outside_work,n,seconds,ops,ops_per_s,ok,load1,extra";
}

struct Row {
    std::string part, impl;
    int threads = 0;
    long long iters = 0;
    long long outside_work = 0;
    long long n = 0;
    double seconds = 0;
    double ops = 0;          // number of completed operations (critical sections, episodes, increments)
    bool ok = true;          // result check of this run (checksum, final counter value, ...)
    std::string extra;
};

inline void print_row(const Row& r) {
    std::printf("%s,%s,%s,%s,%u,%s,%s,%d,%lld,%lld,%lld,%.9f,%.0f,%.3f,%d,%.2f,%s\n",
                host_name().c_str(), arch_name(), os_name(), compiler_name().c_str(), hw_threads(),
                r.part.c_str(), r.impl.c_str(), r.threads, r.iters, r.outside_work, r.n, r.seconds,
                r.ops, r.seconds > 0 ? r.ops / r.seconds : 0.0, r.ok ? 1 : 0, load_avg_1min(),
                r.extra.c_str());
    std::fflush(stdout);
}

}  // namespace pc
