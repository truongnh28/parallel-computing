// backoff.hpp -- waiting policies used after a failed attempt (failed test-and-set, failed CAS,
// barrier not yet released). A policy object lives on the stack of the waiting thread:
//
//     Policy b;                 // fresh state for this acquisition
//     while (!try_once()) b.pause();
//
// Every policy has the same two members, so locks, barriers and counters take the policy as a
// template parameter and the choice costs nothing at run time.
#pragma once

#include <chrono>
#include <cstdint>
#include <functional>
#include <thread>

#include "platform.hpp"

// Bounds of the exponential backoff. Override with -DPC_SLEEP_MIN_NS=... etc. to tune.
#ifndef PC_SLEEP_MIN_NS
#define PC_SLEEP_MIN_NS 100u          // first sleep request, nanoseconds
#endif
#ifndef PC_SLEEP_MAX_NS
#define PC_SLEEP_MAX_NS 100000u       // MAX_DELAY of the assignment text, nanoseconds
#endif
#ifndef PC_SPIN_MIN
#define PC_SPIN_MIN 4u                // first spin window, in cpu_relax() calls
#endif
#ifndef PC_SPIN_MAX
#define PC_SPIN_MAX 1024u             // largest spin window
#endif

namespace pc {

// No backoff: one relax hint per failed attempt.
struct NoBackoff {
    static constexpr const char* name = "none";
    void pause() noexcept { cpu_relax(); }
};

// The exponential backoff printed in the assignment: sleep `delay` nanoseconds, then double the
// delay until it reaches MAX_DELAY. sleep_for is a system call, so the real waiting time is set
// by the OS timer and scheduler, not by `delay` (measured in bench/sleep_latency.cpp).
struct SleepBackoff {
    static constexpr const char* name = "sleep";
    unsigned delay = PC_SLEEP_MIN_NS;
    void pause() {
        std::this_thread::sleep_for(std::chrono::nanoseconds(delay));
        if (delay < PC_SLEEP_MAX_NS) delay *= 2;
    }
};

// Exponential backoff that stays in user space: spin for a random number of relax hints inside
// a window that doubles after every failure. Randomisation keeps waiters from retrying in step.
struct SpinBackoff {
    static constexpr const char* name = "spin";
    unsigned limit = PC_SPIN_MIN;
    std::uint32_t rng = seed();
    void pause() noexcept {
        rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;          // xorshift32
        const unsigned spins = 1 + rng % limit;
        for (unsigned i = 0; i < spins; ++i) cpu_relax();
        if (limit < PC_SPIN_MAX) limit *= 2;
    }
  private:
    static std::uint32_t seed() noexcept {
        static thread_local std::uint32_t s =
            static_cast<std::uint32_t>(std::hash<std::thread::id>{}(std::this_thread::get_id())) | 1u;
        s = s * 1664525u + 1013904223u;
        return s | 1u;
    }
};

// Spin a little, then give the core away. Useful when there are more threads than cores:
// a waiter that keeps spinning may be occupying the core the lock holder needs.
struct YieldBackoff {
    static constexpr const char* name = "yield";
    unsigned spins = 0;
    void pause() noexcept {
        if (spins < 64) { ++spins; cpu_relax(); }
        else std::this_thread::yield();
    }
};

}  // namespace pc
