// lock.h -- Part 1 of the assignment: mutex locks.
//
// Every lock in this file has exactly the interface of the assignment's skeleton:
//
//     void acquire();     // returns when the calling thread owns the lock
//     void release();     // must be called by the thread that owns the lock
//
// Locks written for the assignment (spinlocks: a waiting thread never blocks in the kernel,
// except inside the sleep-based backoff):
//     lock_tas                 test-and-set
//     lock_ttas                test-and-test-and-set
//     lock_tas_bo<Backoff>     TAS  + backoff after every failed attempt
//     lock_ttas_bo<Backoff>    TTAS + backoff after every failed attempt
//     lock_mcs                 MCS queue lock (FIFO)
// Library locks wrapped in the same interface, used as baselines:
//     lock_std_mutex, lock_pthread_mutex, lock_os_unfair (macOS), lock_pthread_spin (Linux),
//     lock_omp (only when compiled with OpenMP)
//
// Rules followed by all locks written here:
//   * Memory order. Taking the lock uses memory_order_acquire and giving it up uses
//     memory_order_release. This pair is what makes everything the previous owner wrote inside
//     its critical section visible to the next owner. memory_order_relaxed is used only where
//     the value read or written cannot affect correctness; each such place has a comment.
//   * Memory layout. The shared state of a lock is aligned to a cache line (pc::kCacheLine) and,
//     because of that alignment, sizeof(lock) is a whole number of cache lines. The lock word
//     therefore never shares a cache line with another variable (no false sharing).
//   * Locks cannot be copied or moved (std::atomic is not copyable; the wrappers delete copying).
//
// Known limitation of lock_mcs: because acquire() and release() take no argument, the queue node
// of a thread is a thread_local variable shared by ALL lock_mcs objects. A thread can therefore
// hold at most ONE lock_mcs at a time (nested MCS locks are not supported), and the lock must be
// released by the thread that acquired it. Using different lock_mcs objects one after the other
// is fine.
//
// Mutation testing: the blocks marked "MUTANT" are deliberately broken variants. They are
// compiled only when the corresponding PC_MUTANT_... macro is defined (make mutation-part1) and
// exist to show that the tests in tests/test_locks.cpp detect such bugs. Without those macros
// the file contains only the correct code.
#pragma once

#include <atomic>
#include <mutex>

#include <pthread.h>

#if defined(__APPLE__)
#include <os/lock.h>          // os_unfair_lock
#endif
#if defined(_OPENMP)
#include <omp.h>              // omp_lock_t
#endif

#include "backoff.hpp"
#include "platform.hpp"

namespace pc {

// ================================================================================================
// TAS: test-and-set lock.
// State: one flag, true = held. exchange(true) atomically writes true and returns the old value:
// if the old value was false, this thread changed the flag from free to held and owns the lock.
// Every attempt is a write, so every waiting thread keeps pulling the cache line of the flag into
// its own cache in exclusive state, even though the attempt fails.
// ================================================================================================
class lock_tas {
  public:
    void acquire() {
#if defined(PC_MUTANT_TAS_CHECK_THEN_SET)
        // MUTANT: "test" and "set" are two separate operations. Two threads can both read
        // false and both store true: both enter the critical section.
        while (locked_.load(std::memory_order_acquire)) cpu_relax();
        locked_.store(true, std::memory_order_release);
#else
        while (locked_.exchange(true, std::memory_order_acquire)) cpu_relax();
#endif
    }

    void release() {
#if defined(PC_MUTANT_TAS_RELEASE_RELAXED)
        // MUTANT: without release ordering nothing guarantees that the writes of the critical
        // section are visible to the next owner (a real bug on ARM/POWER; on x86 the generated
        // code is the same as the correct one).
        locked_.store(false, std::memory_order_relaxed);
#else
        locked_.store(false, std::memory_order_release);
#endif
    }

  private:
    alignas(kCacheLine) std::atomic<bool> locked_{false};
};

// ================================================================================================
// TTAS: test-and-test-and-set lock.
// Same state as TAS. A waiting thread first only READS the flag and attempts the exchange only
// when the flag looks free. Reads leave the cache line in shared state, so waiting threads do not
// disturb each other or the owner. When the lock is released all waiters still see "free" at the
// same time and all attempt the exchange (one wins, the others go back to reading).
// ================================================================================================
class lock_ttas {
  public:
    void acquire() {
        for (;;) {
            // relaxed: this read is only a hint for when to try. Ownership is decided by the
            // exchange below, which uses acquire ordering; a stale value costs one more loop.
            while (locked_.load(std::memory_order_relaxed)) cpu_relax();
            if (!locked_.exchange(true, std::memory_order_acquire)) return;
        }
    }

    void release() { locked_.store(false, std::memory_order_release); }

  private:
    alignas(kCacheLine) std::atomic<bool> locked_{false};
};

// ================================================================================================
// TAS with backoff. After a failed exchange the thread waits according to the policy `Backoff`
// (see backoff.hpp) before trying again. The first attempt is made before the policy object is
// created, so an uncontended acquire costs the same as in lock_tas.
// ================================================================================================
template <class Backoff>
class lock_tas_bo {
  public:
    void acquire() {
        if (!locked_.exchange(true, std::memory_order_acquire)) return;   // free: no waiting needed
        Backoff backoff;                                                  // fresh state per acquisition
        do {
            backoff.pause();
        } while (locked_.exchange(true, std::memory_order_acquire));
    }

    void release() { locked_.store(false, std::memory_order_release); }

  private:
    alignas(kCacheLine) std::atomic<bool> locked_{false};
};

// ================================================================================================
// TTAS with backoff (the "BackoffLock" of Herlihy & Shavit). The thread waits by reading until
// the lock looks free, then attempts the exchange. A failed exchange means that several threads
// saw the lock free at the same moment, i.e. contention is high: only then does the thread back
// off. While the lock is simply held, the thread keeps reading and does not back off.
// ================================================================================================
template <class Backoff>
class lock_ttas_bo {
  public:
    void acquire() {
        if (try_acquire()) return;          // uncontended path: no policy object is created
        Backoff backoff;
        for (;;) {
            backoff.pause();
            if (try_acquire()) return;
        }
    }

    void release() { locked_.store(false, std::memory_order_release); }

  private:
    // Waits until the lock looks free, then makes ONE attempt to take it.
    bool try_acquire() {
        // relaxed: a hint only, exactly as in lock_ttas; the exchange decides.
        while (locked_.load(std::memory_order_relaxed)) cpu_relax();
        return !locked_.exchange(true, std::memory_order_acquire);
    }

    alignas(kCacheLine) std::atomic<bool> locked_{false};
};

// Names for the backoff variants that are tested and measured.
using lock_tas_bo_sleep  = lock_tas_bo<SleepBackoff>;    // the backoff printed in the assignment
using lock_tas_bo_spin   = lock_tas_bo<SpinBackoff>;
using lock_tas_bo_yield  = lock_tas_bo<YieldBackoff>;
using lock_ttas_bo_sleep = lock_ttas_bo<SleepBackoff>;
using lock_ttas_bo_spin  = lock_ttas_bo<SpinBackoff>;
using lock_ttas_bo_yield = lock_ttas_bo<YieldBackoff>;

// ================================================================================================
// MCS queue lock (Mellor-Crummey and Scott).
// Waiting threads form a linked list. Each thread waits on a flag in ITS OWN node, so a release
// writes to the cache of exactly one waiter, and the lock is handed over in arrival order (FIFO).
//
//   tail_ == nullptr            lock is free
//   tail_ == last node          lock is held; the nodes from the owner to tail_ are linked by next
// ================================================================================================
struct alignas(kCacheLine) mcs_node {
    std::atomic<mcs_node*> next{nullptr};   // the thread queued right behind this one
    std::atomic<bool> locked{false};        // true while this thread must wait
};

class lock_mcs {
  public:
    void acquire() {
        mcs_node* me = &node_;
        // Prepare the node BEFORE it becomes reachable by other threads. The node is reused for
        // every acquisition, so both fields must be reset here.
        // relaxed (both stores): no other thread can reach the node yet. It becomes reachable
        // only through the exchange on tail_ and the store to pred->next below; both have
        // release ordering and are executed after these stores, which publishes them.
        me->next.store(nullptr, std::memory_order_relaxed);
        me->locked.store(true, std::memory_order_relaxed);

        // Append this node to the queue; the old tail is our predecessor.
        //   release half: publishes the two stores above to whoever reads tail_ later;
        //   acquire half: if the lock was free (pred == nullptr) we now own it, and this pairs
        //                 with the release-ordered compare-exchange of the previous owner.
        mcs_node* pred = tail_.exchange(me, std::memory_order_acq_rel);
        if (pred == nullptr) return;                           // the lock was free

        pred->next.store(me, std::memory_order_release);       // link behind the predecessor
        // Wait for the predecessor to hand the lock over. acquire pairs with its release store.
        while (me->locked.load(std::memory_order_acquire)) cpu_relax();
    }

    void release() {
        mcs_node* me = &node_;
        mcs_node* succ = me->next.load(std::memory_order_acquire);
        if (succ == nullptr) {
            // Nobody is linked behind us. If tail_ is still our node the queue is really empty:
            // set it back to nullptr (lock free) and return.
            //   success order release: pairs with the acquire half of the next thread's exchange.
            //   failure order relaxed: on failure the loaded value is not used at all; we only
            //                          learn that some other thread has already appended itself.
            mcs_node* expected = me;
            if (tail_.compare_exchange_strong(expected, nullptr, std::memory_order_release,
                                              std::memory_order_relaxed))
                return;
#if defined(PC_MUTANT_MCS_NO_WAIT_SUCCESSOR)
            return;     // MUTANT: leaves without handing over; the thread behind us waits for ever
#endif
            // A thread has swapped itself into tail_ but has not yet stored into our `next`.
            // It will do so in its next step: wait for the link to appear.
            while ((succ = me->next.load(std::memory_order_acquire)) == nullptr) cpu_relax();
        }
#if defined(PC_MUTANT_MCS_ALWAYS_CLEAR_TAIL)
        // MUTANT: marks the lock as free although threads are queued. A newly arriving thread
        // then enters together with the successor that receives the lock below.
        tail_.store(nullptr, std::memory_order_release);
#endif
        succ->locked.store(false, std::memory_order_release);   // hand the lock to the successor
    }

  private:
    alignas(kCacheLine) std::atomic<mcs_node*> tail_{nullptr};

    // One queue node per thread, shared by all lock_mcs objects (see the limitation in the file
    // header). After release() returns no other thread holds a pointer to this node, so the
    // node may safely disappear when its thread exits.
    inline static thread_local mcs_node node_;
};

// ================================================================================================
// Baselines: library locks behind the same two functions. The calls are inlined, so the wrapper
// adds no cost; only the alignment differs from a bare library lock.
// ================================================================================================

// C++ standard library mutex (a pthread mutex on both macOS and Linux).
class lock_std_mutex {
  public:
    void acquire() { m_.lock(); }
    void release() { m_.unlock(); }

  private:
    alignas(kCacheLine) std::mutex m_;
};

// POSIX threads mutex, default attributes.
class lock_pthread_mutex {
  public:
    lock_pthread_mutex() { pthread_mutex_init(&m_, nullptr); }
    ~lock_pthread_mutex() { pthread_mutex_destroy(&m_); }
    lock_pthread_mutex(const lock_pthread_mutex&) = delete;
    lock_pthread_mutex& operator=(const lock_pthread_mutex&) = delete;

    void acquire() { pthread_mutex_lock(&m_); }
    void release() { pthread_mutex_unlock(&m_); }

  private:
    alignas(kCacheLine) pthread_mutex_t m_;
};

#if defined(__APPLE__)
// macOS: os_unfair_lock, the replacement for the removed OSSpinLock. A waiter blocks in the
// kernel, and the kernel knows the owner (priority inheritance). macOS has no pthread_spinlock_t.
class lock_os_unfair {
  public:
    lock_os_unfair() = default;
    lock_os_unfair(const lock_os_unfair&) = delete;
    lock_os_unfair& operator=(const lock_os_unfair&) = delete;

    void acquire() { os_unfair_lock_lock(&l_); }
    void release() { os_unfair_lock_unlock(&l_); }

  private:
    alignas(kCacheLine) os_unfair_lock l_ = OS_UNFAIR_LOCK_INIT;
};
#endif

#if defined(__linux__)
// Linux: POSIX spinlock (glibc implements it as a TTAS-style loop).
class lock_pthread_spin {
  public:
    lock_pthread_spin() { pthread_spin_init(&s_, PTHREAD_PROCESS_PRIVATE); }
    ~lock_pthread_spin() { pthread_spin_destroy(&s_); }
    lock_pthread_spin(const lock_pthread_spin&) = delete;
    lock_pthread_spin& operator=(const lock_pthread_spin&) = delete;

    void acquire() { pthread_spin_lock(&s_); }
    void release() { pthread_spin_unlock(&s_); }

  private:
    alignas(kCacheLine) pthread_spinlock_t s_;
};
#endif

#if defined(_OPENMP)
// OpenMP lock. Compiled only with -fopenmp (the Makefile adds the flag when the compiler has it).
class lock_omp {
  public:
    lock_omp() { omp_init_lock(&l_); }
    ~lock_omp() { omp_destroy_lock(&l_); }
    lock_omp(const lock_omp&) = delete;
    lock_omp& operator=(const lock_omp&) = delete;

    void acquire() { omp_set_lock(&l_); }
    void release() { omp_unset_lock(&l_); }

  private:
    alignas(kCacheLine) omp_lock_t l_;
};
#endif

}  // namespace pc

// The assignment's skeleton declares the lock without a namespace ("lock_mcs lock;").
using pc::lock_mcs;
using pc::lock_tas;
using pc::lock_ttas;

// ================================================================================================
// The list of all locks available on this platform, as (type, name) pairs. A program that wants
// to do something for every lock defines a macro X(type, name) and expands PC_FOR_EACH_LOCK(X);
// each lock then gets its own template instantiation (no virtual call).
// tests/test_locks.cpp and bench/bench_lock.cpp both use this list, so they cannot disagree.
// ================================================================================================
#define PC_LOCKS_PORTABLE(X)                      \
    X(pc::lock_tas, "tas")                        \
    X(pc::lock_ttas, "ttas")                      \
    X(pc::lock_mcs, "mcs")                        \
    X(pc::lock_tas_bo_sleep, "tas_bo_sleep")      \
    X(pc::lock_tas_bo_spin, "tas_bo_spin")        \
    X(pc::lock_tas_bo_yield, "tas_bo_yield")      \
    X(pc::lock_ttas_bo_sleep, "ttas_bo_sleep")    \
    X(pc::lock_ttas_bo_spin, "ttas_bo_spin")      \
    X(pc::lock_ttas_bo_yield, "ttas_bo_yield")    \
    X(pc::lock_std_mutex, "std_mutex")            \
    X(pc::lock_pthread_mutex, "pthread_mutex")

#if defined(__APPLE__)
#define PC_LOCKS_PLATFORM(X) X(pc::lock_os_unfair, "os_unfair_lock")
#elif defined(__linux__)
#define PC_LOCKS_PLATFORM(X) X(pc::lock_pthread_spin, "pthread_spinlock")
#else
#define PC_LOCKS_PLATFORM(X)
#endif

#if defined(_OPENMP)
#define PC_LOCKS_OPENMP(X) X(pc::lock_omp, "omp_lock")
#else
#define PC_LOCKS_OPENMP(X)
#endif

#define PC_FOR_EACH_LOCK(X) PC_LOCKS_PORTABLE(X) PC_LOCKS_PLATFORM(X) PC_LOCKS_OPENMP(X)
