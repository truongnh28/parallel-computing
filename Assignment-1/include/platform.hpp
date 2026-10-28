// platform.hpp -- platform-dependent pieces shared by all three parts of the assignment:
// cache-line size, the CPU "relax" hint used inside spin loops, a monotonic clock,
// and a description of the machine that is written into every CSV row.
#pragma once

#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>

#include <unistd.h>

#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
#endif

namespace pc {

// Size used to pad shared variables so that two of them never share a cache line.
// Apple Silicon reports hw.cachelinesize = 128; x86-64 and most other ARM cores use 64.
#if defined(__APPLE__) && defined(__aarch64__)
inline constexpr std::size_t kCacheLine = 128;
#else
inline constexpr std::size_t kCacheLine = 64;
#endif

// Hint to the core that this thread is in a spin-wait loop.
//  - x86: PAUSE.
//  - AArch64: ISB by default. YIELD is architecturally the "right" hint but is a no-op on most
//    cores (including Apple's), so it gives the lock holder no room. Define PC_RELAX_YIELD to
//    build with YIELD instead and compare.
inline void cpu_relax() noexcept {
#if defined(__x86_64__) || defined(__i386__)
    _mm_pause();
#elif defined(__aarch64__)
#if defined(PC_RELAX_YIELD)
    asm volatile("yield" ::: "memory");
#else
    asm volatile("isb" ::: "memory");
#endif
#endif
}

inline double now_s() noexcept {
    using clk = std::chrono::steady_clock;
    return std::chrono::duration<double>(clk::now().time_since_epoch()).count();
}

// 1-minute load average, or -1 if unavailable. Recorded with every measurement so that runs
// taken on a busy machine can be recognised afterwards.
inline double load_avg_1min() noexcept {
    double v[1];
    return getloadavg(v, 1) == 1 ? v[0] : -1.0;
}

inline std::string host_name() {
    if (const char* h = std::getenv("PC_HOST")) return h;   // lets a script give the machine a stable id
    char buf[256] = {0};
    if (gethostname(buf, sizeof buf - 1) != 0) return "unknown";
    std::string s(buf);
    if (auto dot = s.find('.'); dot != std::string::npos) s.resize(dot);
    return s;
}

inline const char* arch_name() noexcept {
#if defined(__aarch64__)
    return "arm64";
#elif defined(__x86_64__)
    return "x86_64";
#else
    return "other";
#endif
}

inline const char* os_name() noexcept {
#if defined(__APPLE__)
    return "macOS";
#elif defined(__linux__)
    return "Linux";
#else
    return "other";
#endif
}

inline std::string compiler_name() {
#if defined(__clang__)
    return std::string("clang-") + std::to_string(__clang_major__) + "." + std::to_string(__clang_minor__);
#elif defined(__GNUC__)
    return std::string("gcc-") + std::to_string(__GNUC__) + "." + std::to_string(__GNUC_MINOR__);
#else
    return "unknown";
#endif
}

inline unsigned hw_threads() noexcept {
    unsigned n = std::thread::hardware_concurrency();
    return n ? n : 1;
}

}  // namespace pc
