// barrier.hpp -- Part 2: barrier synchronization.
//
// A barrier for n threads: no thread returns from its k-th call of wait() before all n threads
// have made their k-th call. One such collective crossing is called an *episode*.
//
// Every class below has the same interface, so the test and the benchmark are written once:
//
//     Barrier b(n);          // n = fixed number of participating threads
//     b.wait(tid);           // called by thread tid, 0 <= tid < n, once per episode
//
// Own implementations (the subject of the assignment):
//   SenseBarrier<Policy>   centralized sense-reversing barrier; waiting threads poll the shared
//                          flag and call Policy::pause() between two polls (see backoff.hpp):
//                          NoBackoff = pure spinning, YieldBackoff, SleepBackoff, SpinBackoff.
//   SenseBarrierBlocking   the same protocol, but waiting threads block in the kernel with
//                          C++20 std::atomic::wait and are woken with notify_all.
// Baselines (existing barriers, for comparison):
//   StdBarrier             std::barrier (C++20)
//   CondVarBarrier         mutex + condition variable + generation counter (the classic way to
//                          build a barrier from Pthreads primitives; works on every platform)
//   PthreadBarrier         pthread_barrier_t                 (Linux only: macOS does not have it)
//   OmpBarrier             #pragma omp barrier               (only when compiled with OpenMP)
//
// Why wait() takes the thread id. The sense-reversing barrier needs one private bit per thread
// *and per barrier* (the "local sense"). Three places could hold it:
//   - a thread_local variable: wait() would need no argument, but the bit would be shared by all
//     barrier objects the thread uses, and two barriers used alternately would corrupt each other;
//   - a variable owned by the caller and passed by reference: correct, but every caller has to
//     manage one variable per barrier;
//   - an array inside the barrier, indexed by thread id: one barrier object is self-contained and
//     a thread can use any number of barriers. This is the choice made here. The price is that
//     the caller must give each participant a distinct id in [0, n).
#pragma once

#include <atomic>
#include <barrier>
#include <condition_variable>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <mutex>
#include <vector>

#ifdef __linux__
#include <pthread.h>
#endif

#include "backoff.hpp"
#include "platform.hpp"

// Test hook. PC_BARRIER_TEST_POINT() stands between the shared-memory steps of the
// sense-reversing protocol. A test may define it before including this header to insert random
// delays at these points and so force interleavings that a normal run almost never produces
// (tests/test_barrier.cpp does this when compiled with -DPC_BARRIER_FUZZ). In every other build,
// including the benchmark, it expands to nothing.
#ifndef PC_BARRIER_TEST_POINT
#define PC_BARRIER_TEST_POINT() ((void)0)
#endif

namespace pc {

// The private state of one thread in one barrier. alignas pads every element to a full cache
// line: each thread writes its own element once per episode, and without the padding these
// writes would invalidate the line under its neighbours (false sharing).
struct alignas(kCacheLine) BarrierLocal {
    bool sense = true;      // the value the shared flag will have when this thread may leave
};

// ------------------------------------------------------------------------------------------------
// Centralized sense-reversing barrier.
//
// Shared state: count_ = number of threads that have arrived in the current episode,
//               sense_ = a flag that is flipped (never "reset") once per episode.
// Initially sense_ == false and every local sense == true.
//
// Invariant between episodes: every local sense == !sense_. A thread therefore waits for
// "sense_ == my local sense". Only the last arriver of the current episode makes that true, and
// it cannot become false again before this thread has left: the next flip needs n arrivals in
// the next episode, and one of them is this thread's.
// ------------------------------------------------------------------------------------------------
template <class Policy = NoBackoff>
class SenseBarrier {
  public:
    explicit SenseBarrier(int n) : n_(n), local_(static_cast<std::size_t>(n)) {}

    void wait(int tid) {
        BarrierLocal& me = local_[static_cast<std::size_t>(tid)];
        const bool my_sense = me.sense;

        // Arrive. fetch_add returns the value *before* the increment, so the n-th (last) arriver
        // reads n - 1.
        //   release: everything this thread wrote before the barrier is published to the threads
        //            that arrive after it, in particular to the last arriver;
        //   acquire: the last arriver takes over what all earlier arrivers published. It needs
        //            that for its own reads after the barrier (it never reads sense_), and it
        //            passes it on to the waiting threads with the release store on sense_ below.
        // Both halves are needed for "every write before the barrier is visible to every thread
        // after the barrier", hence acq_rel. With release alone the test catches the last
        // arriver reading stale data on Apple Silicon (docs/part2.md, mutant 4).
        if (count_.fetch_add(1, std::memory_order_acq_rel) == n_ - 1) {
            // Last arriver: prepare the next episode, THEN release the others. In this order a
            // released thread can never meet the old counter value.
            PC_BARRIER_TEST_POINT();
            // relaxed: this store is sequenced before the release store on sense_, and a thread
            // starts its next episode only after it has read that new sense_ value with acquire
            // (or it is this thread itself), so its next fetch_add is ordered after this store.
            count_.store(0, std::memory_order_relaxed);
            PC_BARRIER_TEST_POINT();
            // release: publishes the counter reset and everything acquired by the fetch_add.
            sense_.store(my_sense, std::memory_order_release);
        } else {
            // Not the last: wait until the last arriver flips the shared flag to my sense.
            // acquire: pairs with the release store above; after the loop this thread sees the
            // counter reset and all writes made before the barrier by all threads.
            Policy backoff;                       // fresh backoff state for every episode
            while (sense_.load(std::memory_order_acquire) != my_sense) backoff.pause();
        }

        PC_BARRIER_TEST_POINT();
        // Reverse my sense for the next episode. Private to this thread: no atomic needed.
        me.sense = !my_sense;
    }

    int participants() const noexcept { return n_; }

  private:
    // Read-only after construction (the vector object holds only a pointer to the padded
    // per-thread slots, which live on the heap).
    const int n_;
    std::vector<BarrierLocal> local_;
    // Written by every arriving thread. Own cache line: an arrival must not invalidate the line
    // that the waiting threads are polling.
    alignas(kCacheLine) std::atomic<int> count_{0};
    // Read by all waiting threads, written once per episode. Own cache line.
    alignas(kCacheLine) std::atomic<bool> sense_{false};
};

// ------------------------------------------------------------------------------------------------
// The same sense-reversing protocol with blocking waits.
//
// sense_.wait(old) blocks the calling thread as long as sense_ == old (a futex on Linux,
// __ulock on macOS); notify_all wakes every thread blocked on sense_. No wake-up can be lost:
// wait() re-checks the value atomically with going to sleep, so a flip that happens between the
// load and the wait is noticed.
// Note that the standard libraries poll for a short time before they really block, so this
// variant is "spin briefly, then block" in practice.
// ------------------------------------------------------------------------------------------------
class SenseBarrierBlocking {
  public:
    explicit SenseBarrierBlocking(int n) : n_(n), local_(static_cast<std::size_t>(n)) {}

    void wait(int tid) {
        BarrierLocal& me = local_[static_cast<std::size_t>(tid)];
        const bool my_sense = me.sense;

        // Memory orders: exactly as in SenseBarrier::wait, for the same reasons.
        if (count_.fetch_add(1, std::memory_order_acq_rel) == n_ - 1) {
            PC_BARRIER_TEST_POINT();
            // relaxed: published by the release store on sense_ that follows (see SenseBarrier).
            count_.store(0, std::memory_order_relaxed);
            PC_BARRIER_TEST_POINT();
            sense_.store(my_sense, std::memory_order_release);
            PC_BARRIER_TEST_POINT();
            sense_.notify_all();                  // wake the threads blocked in wait() below
        } else {
            // wait(old) may also return without a notification (spurious wake-up), so the
            // condition is checked in a loop. The value this thread waits for is stable: once
            // sense_ == my_sense it stays so until this thread has left.
            while (sense_.load(std::memory_order_acquire) != my_sense)
                sense_.wait(!my_sense, std::memory_order_acquire);
        }
        PC_BARRIER_TEST_POINT();
        me.sense = !my_sense;
    }

    int participants() const noexcept { return n_; }

  private:
    const int n_;
    std::vector<BarrierLocal> local_;
    alignas(kCacheLine) std::atomic<int> count_{0};
    alignas(kCacheLine) std::atomic<bool> sense_{false};
};

// ------------------------------------------------------------------------------------------------
// Baseline: std::barrier (C++20). The thread id is not needed.
// ------------------------------------------------------------------------------------------------
class StdBarrier {
  public:
    explicit StdBarrier(int n) : barrier_(n) {}
    void wait(int /*tid*/) { barrier_.arrive_and_wait(); }

  private:
    std::barrier<> barrier_;
};

// ------------------------------------------------------------------------------------------------
// Baseline: the classic barrier built from a mutex and a condition variable.
//
// generation_ counts the completed episodes. It plays the role of the sense flag: a waiting
// thread remembers the generation it arrived in and sleeps until the generation has changed.
// Without it, a woken thread could not tell "my episode is complete" from "the counter was
// reset and threads of the next episode are arriving" (the same reuse problem that sense
// reversal solves), and it could not recognise spurious wake-ups either.
// ------------------------------------------------------------------------------------------------
class CondVarBarrier {
  public:
    explicit CondVarBarrier(int n) : n_(n) {}

    void wait(int /*tid*/) {
        std::unique_lock<std::mutex> lock(mutex_);
        if (++count_ == n_) {
            count_ = 0;                           // last arriver: start the next episode
            ++generation_;
            lock.unlock();                        // wake the others without holding the mutex
            cv_.notify_all();
        } else {
            const unsigned long my_generation = generation_;
            cv_.wait(lock, [&] { return generation_ != my_generation; });
        }
    }

  private:
    const int n_;
    std::mutex mutex_;
    std::condition_variable cv_;
    int count_ = 0;                               // protected by mutex_
    unsigned long generation_ = 0;                // protected by mutex_
};

// ------------------------------------------------------------------------------------------------
// Baseline: pthread_barrier_t. POSIX marks barriers as optional and macOS does not provide them.
// ------------------------------------------------------------------------------------------------
#ifdef __linux__
#define PC_HAVE_PTHREAD_BARRIER 1
class PthreadBarrier {
  public:
    explicit PthreadBarrier(int n) {
        if (pthread_barrier_init(&barrier_, nullptr, static_cast<unsigned>(n)) != 0) {
            std::fprintf(stderr, "pthread_barrier_init failed\n");
            std::abort();
        }
    }
    ~PthreadBarrier() { pthread_barrier_destroy(&barrier_); }
    PthreadBarrier(const PthreadBarrier&) = delete;
    PthreadBarrier& operator=(const PthreadBarrier&) = delete;

    void wait(int /*tid*/) {
        // Returns PTHREAD_BARRIER_SERIAL_THREAD in exactly one thread per episode, 0 in the others.
        const int rc = pthread_barrier_wait(&barrier_);
        if (rc != 0 && rc != PTHREAD_BARRIER_SERIAL_THREAD) {
            std::fprintf(stderr, "pthread_barrier_wait failed\n");
            std::abort();
        }
    }

  private:
    pthread_barrier_t barrier_;
};
#endif  // __linux__

// ------------------------------------------------------------------------------------------------
// Baseline: the OpenMP barrier.
//
// "#pragma omp barrier" synchronises the threads of the innermost enclosing OpenMP parallel
// region. It does nothing useful in a thread created with std::thread (such a thread forms a
// team of one). OmpBarrier::wait must therefore be called only by the threads of an
// "omp parallel" region whose team has exactly n threads; the test and the benchmark have a
// separate launcher for it. The number of participants is fixed by the team, not by this object.
// ------------------------------------------------------------------------------------------------
#ifdef _OPENMP
#define PC_HAVE_OMP_BARRIER 1
class OmpBarrier {
  public:
    explicit OmpBarrier(int /*n*/) {}
    void wait(int /*tid*/) {
        #pragma omp barrier
    }
};
#endif  // _OPENMP

}  // namespace pc
