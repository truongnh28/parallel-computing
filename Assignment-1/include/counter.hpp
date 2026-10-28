// counter.hpp -- Part 3: concurrent counters built with the three kinds of synchronisation of the
// lecture: a mutex lock, compare-and-swap, and fetch-and-add.
//
// Specification shared by every counter in this file
// --------------------------------------------------
//   long increment();   adds 1 and returns the value the counter had BEFORE this increment
//   long get();         returns the current value
//
// The counter starts at 0. If increment() is called M times in total (by any number of threads),
// the M calls return the values 0, 1, ..., M-1, each exactly once, and get() then returns M.
// Returning the old value costs nothing extra (all three primitives produce it anyway) and it is
// what lets the test check much more than the final value (tests/test_counter.cpp).
//
// Memory orders (used by counter_cas and counter_faa; the reasoning is in docs/part3.md)
// --------------------------------------------------------------------------------------
// The default order is memory_order_relaxed. Two different things must be kept apart:
//   * ATOMICITY of the read-modify-write. fetch_add and compare_exchange are atomic with every
//     memory order: no other write to the same variable can fall between the read and the write.
//     The C++ standard also keeps ONE total order of all writes to one atomic variable (its
//     "modification order"), again with every memory order. These two facts alone give the
//     specification above, so nothing in the counter itself breaks with relaxed.
//   * ORDERING against OTHER memory locations. This is the only thing acquire / release /
//     seq_cst add. The counter protects no data besides its own value, so it does not need it.
// A stronger order would matter only to a caller who uses the counter to publish other data
// ("I wrote slot k, then incremented; a reader that sees the count may read slot k"). Such a
// caller can instantiate the counter with a stronger order: the order is a template parameter,
// which also lets the benchmark measure what the stronger order costs (variants *_seqcst).
#pragma once

#include <atomic>
#include <cstdint>

#include "backoff.hpp"
#include "platform.hpp"

namespace pc {

// ---- (a) mutex lock -----------------------------------------------------------------------------
// A plain integer; every access is inside the critical section of a lock of type L.
// L is any type with two member functions, void acquire() and void release() (the interface of
// include/lock.h). The counter is exactly as correct as the lock: if L fails to give mutual
// exclusion, increments are lost.
template <class L>
class counter_lock {
    // The lock and the value are on two different cache lines. While one thread is in the
    // critical section, the waiting threads of a spinlock keep reading the lock word; if the
    // value were on the same line, each write to the value would invalidate their copies of it.
    // `mutable` because get() is logically read-only but still has to take the lock.
    alignas(kCacheLine) mutable L lock_;
    alignas(kCacheLine) long value_ = 0;

  public:
    long increment() {
        lock_.acquire();
        const long old = value_;      // read ...
        value_ = old + 1;             // ... and write inside the same critical section
        lock_.release();
        return old;
    }

    // The read also takes the lock: value_ is not atomic, so reading it while another thread
    // writes it would be a data race (undefined behaviour in C++, and ThreadSanitizer reports it).
    long get() const {
        lock_.acquire();
        const long v = value_;
        lock_.release();
        return v;
    }
};

// ---- (b) compare-and-swap -----------------------------------------------------------------------
// Read the value, then try to replace it by value + 1 with a CAS. The CAS fails if another thread
// changed the value in between; then the thread waits according to Policy (backoff.hpp) and tries
// again with the value the failed CAS has just read. This is lock-free but not wait-free: one
// thread always succeeds, but a given thread may fail any number of times.
//
// Policy: NoBackoff (retry at once), SleepBackoff (the assignment's sample: sleep, double the
// delay), SpinBackoff (wait in user space), YieldBackoff.
// Order : memory order of a successful CAS and of get(); see the top of the file.
template <class Policy = NoBackoff, std::memory_order Order = std::memory_order_relaxed>
class counter_cas {
    alignas(kCacheLine) std::atomic<long> value_{0};   // alone on its cache line (the class is
                                                       // aligned and padded to kCacheLine)

    // Number of failed CAS attempts made by the calling thread (on any counter of this type).
    // It is thread_local, so counting costs one private add on the failure path only; a shared
    // atomic statistic would add a second contended cache line and change what is measured.
    static inline thread_local std::uint64_t failed_ = 0;

  public:
    long increment() {
        // relaxed: this first read is only a guess; the CAS below checks it and corrects it.
        long old = value_.load(std::memory_order_relaxed);
        Policy backoff;               // fresh backoff state for this increment
        // compare_exchange_weak(expected, desired, success order, failure order):
        //   if value_ == old : value_ = old + 1, returns true;
        //   otherwise        : old = value_ (the value seen now), returns false.
        // "weak" may also fail although value_ == old (possible on LL/SC machines); that is
        // harmless here because the loop retries anyway, and it avoids a hidden inner loop.
        // Failure order relaxed: a failed CAS only refreshes the guess `old`.
        while (!value_.compare_exchange_weak(old, old + 1, Order, std::memory_order_relaxed)) {
            ++failed_;
            backoff.pause();          // "a waiting time before retrying a failed increment attempt"
        }
        return old;                   // the value our successful CAS replaced
    }

    long get() const noexcept { return value_.load(Order); }

    // Failed attempts of the calling thread so far. A thread reads it at the end of its work and
    // the caller adds the per-thread numbers up.
    static std::uint64_t failed_attempts() noexcept { return failed_; }
    static void reset_failed_attempts() noexcept { failed_ = 0; }
};

// ---- (c) fetch-and-add --------------------------------------------------------------------------
// One atomic instruction does the whole increment and hands back the old value. Wait-free: every
// call finishes in a bounded number of its own steps, whatever the other threads do.
template <std::memory_order Order = std::memory_order_relaxed>
class counter_faa_ord {
    alignas(kCacheLine) std::atomic<long> value_{0};

  public:
    // relaxed by default: atomicity of the addition does not depend on the memory order (see top).
    long increment() noexcept { return value_.fetch_add(1, Order); }
    long get() const noexcept { return value_.load(Order); }
};

// The names used in the rest of the project.
using counter_faa        = counter_faa_ord<std::memory_order_relaxed>;
using counter_faa_seqcst = counter_faa_ord<std::memory_order_seq_cst>;
using counter_cas_seqcst = counter_cas<NoBackoff, std::memory_order_seq_cst>;

}  // namespace pc
