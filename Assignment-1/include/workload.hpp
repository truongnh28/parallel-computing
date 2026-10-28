// workload.hpp -- the work unit of the assignment (a naive primality test) and the helpers that
// keep the compiler from deleting it.
//
// In the assignment's skeleton the result of is_prime(N) is discarded, is_prime has no side
// effects and N is a compile-time constant, so at -O2 the compiler may remove the call and leave
// an empty critical section. Three measures prevent that here:
//   1. is_prime is not inlined, so the loop is compiled once, as written. With GCC it is also
//      marked `noipa`: GCC's interprocedural analysis otherwise proves the function pure and
//      replaces a loop of identical calls by one call and a multiplication;
//   2. N comes from the command line at run time, and callers pass it through opaque() at EVERY
//      call, so that no two calls are provably identical;
//   3. every result is added to a per-thread sum that is checked at the end of the run:
//      the sum must equal the number of calls when N is prime.
#pragma once

#include <cstdint>

namespace pc {

// Exactly the function given in the assignment.
#if defined(__GNUC__) && !defined(__clang__)
__attribute__((noinline, noipa))
#else
__attribute__((noinline))
#endif
inline int is_prime(int p) {
    int d;
    for (d = 2; d < p; d = d + 1)
        if (p % d == 0)
            return 0;
    return 1;
}

// Returns its argument, but the compiler cannot see that (the value goes through a register the
// optimiser must treat as modified).
template <class T>
inline T opaque(T v) noexcept {
    asm volatile("" : "+r"(v));
    return v;
}

// N must be prime: for a composite N the loop exits at the first divisor and the "work" is a few
// iterations instead of N - 2. Used by the benchmarks to reject a bad N before measuring.
inline bool is_prime_checked(int p) noexcept {
    if (p < 2) return false;
    for (long long d = 2; d * d <= p; ++d)
        if (p % d == 0) return false;
    return true;
}

}  // namespace pc
