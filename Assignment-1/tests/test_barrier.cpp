// test_barrier.cpp -- correctness test of every barrier in barrier.hpp.
//
// What is checked (see docs/part2.md for the reasoning):
//
//   EPISODE CHECK. In episode e every thread writes e into its own slot, then calls wait().
//   When wait() returns, the thread reads all slots:
//       slot < e      -> a thread has not arrived at barrier e yet, but I am already through:
//                        the barrier let me pass EARLY;
//       slot > e + 1  -> a thread is two episodes ahead of me: it passed barrier e + 1 without me.
//   slot == e + 1 is legal: a thread that left barrier e before me may already be on its way
//   to barrier e + 1.
//
//   HANDOFF CHECK. Two barriers a and b are used alternately: write | a | read all | b.
//   Nothing is written between a and b, so every thread must read exactly the round number
//   from every slot. The slots are PLAIN (non-atomic) variables on purpose: the only thing
//   that orders the writes before the reads is the barrier. Under ThreadSanitizer this checks
//   that the barrier really creates the happens-before relation it promises (memory orders),
//   and it checks that one thread can use two barrier objects.
//
//   NO HANG. A watchdog thread aborts the program if no thread has advanced for stall_s seconds.
//
//   SCHEDULE FUZZING (only when compiled with -DPC_BARRIER_FUZZ). barrier.hpp contains test
//   points between the shared-memory steps of the sense-reversing protocol. In this build every
//   test point inserts a random delay, so that a thread is "caught" between two steps far more
//   often than the scheduler would do it by chance. All checks above stay the same.
//
// Usage:  test_barrier [small] [impl=<name>] [stall_s=<seconds>] [cap_s=<seconds>] [list]
//   small   reduced sizes for the ThreadSanitizer build
//   impl=   test only one variant
// Exit code: 0 = all PASS, 1 = a check failed, 2 = hang caught by the watchdog.
//
// Built with -DPC_MUTANT=<1..5> the program tests one deliberately broken barrier instead
// (mutation testing, see the MUTANTS section below and `make mutation-part2`).
// The two options combine: -DPC_MUTANT=2 -DPC_BARRIER_FUZZ tests mutant 2 with schedule fuzzing.
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <string>
#include <thread>
#include <vector>

#ifdef _OPENMP
#include <omp.h>
#endif

#include "platform.hpp"

// Random delay: mostly nothing, often a short spin, sometimes a yield, rarely a real sleep of up
// to 100 microseconds. Used (a) before a thread arrives at the barrier, so that the arrival order
// and the distance between arrivals change from episode to episode, and (b) at the test points
// inside the barrier in the fuzzing build.
inline void random_delay(std::uint32_t& rng) {
    rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5;          // xorshift32
    const std::uint32_t r = rng;
    if ((r & 1023u) == 0) {
        std::this_thread::sleep_for(std::chrono::microseconds(1 + (r >> 10) % 100));
    } else if ((r & 63u) == 1) {
        std::this_thread::yield();
    } else if ((r & 3u) == 2) {
        const unsigned spins = (r >> 8) % 256;
        for (unsigned i = 0; i < spins; ++i) pc::cpu_relax();
    }
}

// Schedule fuzzing: must be defined before barrier.hpp is included.
#if defined(PC_BARRIER_FUZZ)
inline void barrier_test_point() {
    static thread_local std::uint32_t rng =
        static_cast<std::uint32_t>(std::hash<std::thread::id>{}(std::this_thread::get_id())) | 1u;
    random_delay(rng);
}
#define PC_BARRIER_TEST_POINT() barrier_test_point()
#endif

#include "barrier.hpp"
#include "bench_common.hpp"

// PC_TSAN is defined when this file is compiled with -fsanitize=thread (gcc and clang differ).
#if defined(__SANITIZE_THREAD__)
#define PC_TSAN 1
#elif defined(__has_feature)
#if __has_feature(thread_sanitizer)
#define PC_TSAN 1
#endif
#endif

// ================================================================================================
// MUTANTS -- deliberately broken barriers. NOT production code; compiled only with -DPC_MUTANT=k.
// Each class is a copy of pc::SenseBarrier<NoBackoff> (including its test points) with exactly
// one fault, marked "MUTATION". A good test must reject every one of them.
// ================================================================================================
#if defined(PC_MUTANT)
namespace mutant {

#if PC_MUTANT == 1
// Fault: no sense reversal. One flag "open" is set by the last arriver and closed again by the
// last thread that leaves (counted with a second counter).
// Expected symptom: a fast thread leaves, comes back for the next episode while the flag is
// still open, and runs straight through -> it passes early.
const char* const kName = "single_flag";
class Barrier {
  public:
    explicit Barrier(int n) : n_(n) {}
    void wait(int /*tid*/) {
        if (count_.fetch_add(1) == n_ - 1) {
            PC_BARRIER_TEST_POINT();
            count_.store(0);
            PC_BARRIER_TEST_POINT();
            open_.store(true);                                  // release everybody
        } else {
            while (!open_.load()) pc::cpu_relax();
        }
        PC_BARRIER_TEST_POINT();
        // MUTATION: the flag is reset instead of reversed.
        if (left_.fetch_add(1) == n_ - 1) {                     // I am the last one to leave
            left_.store(0);
            open_.store(false);                                 // close the barrier again
        }
    }
  private:
    const int n_;
    alignas(pc::kCacheLine) std::atomic<int> count_{0};
    alignas(pc::kCacheLine) std::atomic<int> left_{0};
    alignas(pc::kCacheLine) std::atomic<bool> open_{false};
};

#elif PC_MUTANT == 2
// Fault: the last arriver flips the sense BEFORE it resets the counter.
// Expected symptom: a released thread can arrive at the next barrier while the counter still
// holds n; its arrival is then wiped out by the late reset, the next episode never collects
// n arrivals, and all threads hang.
const char* const kName = "flip_before_reset";
class Barrier {
  public:
    explicit Barrier(int n) : n_(n), local_(static_cast<std::size_t>(n)) {}
    void wait(int tid) {
        pc::BarrierLocal& me = local_[static_cast<std::size_t>(tid)];
        const bool my_sense = me.sense;
        if (count_.fetch_add(1, std::memory_order_acq_rel) == n_ - 1) {
            PC_BARRIER_TEST_POINT();
            // MUTATION: these two statements are swapped.
            sense_.store(my_sense, std::memory_order_release);
            PC_BARRIER_TEST_POINT();
            count_.store(0, std::memory_order_release);
        } else {
            while (sense_.load(std::memory_order_acquire) != my_sense) pc::cpu_relax();
        }
        PC_BARRIER_TEST_POINT();
        me.sense = !my_sense;
    }
  private:
    const int n_;
    std::vector<pc::BarrierLocal> local_;
    alignas(pc::kCacheLine) std::atomic<int> count_{0};
    alignas(pc::kCacheLine) std::atomic<bool> sense_{false};
};

#elif PC_MUTANT == 3
// Fault: the arrival counter is updated with a load followed by a store instead of one atomic
// fetch_add.
// Expected symptom: two threads read the same value, one arrival is lost, the counter never
// reaches n and all threads hang. (Two threads can also both read n - 1 ... and so on.)
const char* const kName = "nonatomic_count";
class Barrier {
  public:
    explicit Barrier(int n) : n_(n), local_(static_cast<std::size_t>(n)) {}
    void wait(int tid) {
        pc::BarrierLocal& me = local_[static_cast<std::size_t>(tid)];
        const bool my_sense = me.sense;
        // MUTATION: load + store instead of fetch_add.
        const int position = count_.load(std::memory_order_acquire);
        PC_BARRIER_TEST_POINT();
        count_.store(position + 1, std::memory_order_release);
        if (position == n_ - 1) {
            PC_BARRIER_TEST_POINT();
            count_.store(0, std::memory_order_relaxed);
            PC_BARRIER_TEST_POINT();
            sense_.store(my_sense, std::memory_order_release);
        } else {
            while (sense_.load(std::memory_order_acquire) != my_sense) pc::cpu_relax();
        }
        PC_BARRIER_TEST_POINT();
        me.sense = !my_sense;
    }
  private:
    const int n_;
    std::vector<pc::BarrierLocal> local_;
    alignas(pc::kCacheLine) std::atomic<int> count_{0};
    alignas(pc::kCacheLine) std::atomic<bool> sense_{false};
};

#elif PC_MUTANT == 4
// Fault: fetch_add uses memory_order_release instead of acq_rel, so the last arriver does not
// acquire what the earlier arrivers published.
// Expected symptom: none on real hardware (the machine orders more than C++ requires). By the
// C++ memory model a thread that waited may read data of a thread that arrived after it without
// a happens-before relation: a data race that only ThreadSanitizer can see (handoff check).
const char* const kName = "fetch_add_release_only";
class Barrier {
  public:
    explicit Barrier(int n) : n_(n), local_(static_cast<std::size_t>(n)) {}
    void wait(int tid) {
        pc::BarrierLocal& me = local_[static_cast<std::size_t>(tid)];
        const bool my_sense = me.sense;
        // MUTATION: release instead of acq_rel.
        if (count_.fetch_add(1, std::memory_order_release) == n_ - 1) {
            PC_BARRIER_TEST_POINT();
            count_.store(0, std::memory_order_relaxed);
            PC_BARRIER_TEST_POINT();
            sense_.store(my_sense, std::memory_order_release);
        } else {
            while (sense_.load(std::memory_order_acquire) != my_sense) pc::cpu_relax();
        }
        PC_BARRIER_TEST_POINT();
        me.sense = !my_sense;
    }
  private:
    const int n_;
    std::vector<pc::BarrierLocal> local_;
    alignas(pc::kCacheLine) std::atomic<int> count_{0};
    alignas(pc::kCacheLine) std::atomic<bool> sense_{false};
};

#elif PC_MUTANT == 5
// Fault: the local sense is a thread_local variable, i.e. one bit per thread shared by ALL
// barrier objects, instead of one bit per thread per barrier.
// Expected symptom: correct as long as a thread uses a single barrier; wrong as soon as it
// alternates between two barriers (handoff check).
const char* const kName = "thread_local_sense";
class Barrier {
  public:
    explicit Barrier(int n) : n_(n) {}
    void wait(int /*tid*/) {
        // MUTATION: shared by all Barrier objects that this thread uses.
        static thread_local bool local_sense = true;
        const bool my_sense = local_sense;
        if (count_.fetch_add(1, std::memory_order_acq_rel) == n_ - 1) {
            PC_BARRIER_TEST_POINT();
            count_.store(0, std::memory_order_relaxed);
            PC_BARRIER_TEST_POINT();
            sense_.store(my_sense, std::memory_order_release);
        } else {
            while (sense_.load(std::memory_order_acquire) != my_sense) pc::cpu_relax();
        }
        PC_BARRIER_TEST_POINT();
        local_sense = !my_sense;
    }
  private:
    const int n_;
    alignas(pc::kCacheLine) std::atomic<int> count_{0};
    alignas(pc::kCacheLine) std::atomic<bool> sense_{false};
};

#else
#error "PC_MUTANT must be 1, 2, 3, 4 or 5"
#endif

}  // namespace mutant
#endif  // PC_MUTANT
// ================================================================================================
// End of MUTANTS. Everything below is the test harness.
// ================================================================================================

namespace {

constexpr int kExitFail = 1;      // a check found a wrong episode / wrong value
constexpr int kExitHang = 2;      // the watchdog found that no thread advances

// Settings from the command line.
double g_stall_s = 10.0;          // watchdog: abort when nothing has moved for this long
double g_cap_s = 1.5;             // time budget of one phase
double g_size = 1.0;              // multiplies the episode targets ("small" sets 0.05)
std::string g_only;               // impl=<name>: test only this variant

// One cache line per slot, so that the test itself does not suffer from false sharing.
struct alignas(pc::kCacheLine) AtomicSlot { std::atomic<long> v{0}; };
struct alignas(pc::kCacheLine) PlainSlot  { long v = 0; };

// Prints the message and ends the process immediately. std::_Exit is used because the other
// threads may be stuck inside a broken barrier and can never be joined.
std::atomic<bool> g_dying{false};
[[noreturn]] void die(int code, const std::string& message) {
    if (g_dying.exchange(true))                       // another thread is already reporting
        for (;;) std::this_thread::sleep_for(std::chrono::seconds(1));
    std::printf("%s\n", message.c_str());
    std::fflush(stdout);
    std::_Exit(code);
}

// State of one test phase, shared by the worker threads and the watchdog.
struct Phase {
    std::string variant, name;
    int threads;
    // episode[t] = the episode (or round) thread t has reached; written only by thread t.
    //
    // These slots must be atomic. In the episode check, thread j reads slot i after barrier e
    // while thread i, which also left barrier e, may already be writing e + 1. Nothing orders
    // these two accesses, even with a perfect barrier, so a plain variable would be a data race
    // in the test itself. The accesses use memory_order_relaxed on purpose: the slots then add
    // no ordering of their own, and "slot >= e" can only hold because the barrier ordered the
    // write before the read.
    std::vector<AtomicSlot> episode;
    std::atomic<long> final_episode;                  // all threads stop after this episode
    std::atomic<bool> time_up{false};                 // set by the watchdog when the budget is used
    std::atomic<bool> finished{false};                // set by the main thread after the join

    Phase(const std::string& v, const std::string& n, int p, long target)
        : variant(v), name(n), threads(p), episode(static_cast<std::size_t>(p)), final_episode(target) {}
};

// The watchdog. It (1) tells the workers when the time budget of the phase is used up and
// (2) detects a hang: the sum of all episode numbers has not changed for g_stall_s seconds.
// Progress, not total time, is the criterion, because an oversubscribed spinning barrier can be
// very slow without being wrong.
void watchdog(Phase& ph) {
    const double start = pc::now_s();
    double last_change = start;
    long last_sum = -1;
    while (!ph.finished.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
        const double now = pc::now_s();
        if (now - start > g_cap_s) ph.time_up.store(true);
        long sum = 0;
        for (auto& s : ph.episode) sum += s.v.load(std::memory_order_relaxed);
        if (sum != last_sum) {
            last_sum = sum;
            last_change = now;
        } else if (now - last_change > g_stall_s) {
            std::string msg = "FAIL " + ph.variant + " [" + ph.name + "]: HANG, no thread advanced for " +
                              std::to_string(g_stall_s) + " s (watchdog). Episode reached by each thread:";
            for (auto& s : ph.episode) msg += " " + std::to_string(s.v.load(std::memory_order_relaxed));
            die(kExitHang, msg);
        }
    }
}

// Thread 0 turns "time is up" into a common last episode. It is called by thread 0 after it has
// left barrier e and before it arrives at barrier e + 1. At that moment every other thread is in
// episode e or e + 1 and cannot leave barrier e + 1 (thread 0 is missing there). Setting the last
// episode to e + 1 (or later) is therefore seen by every thread before it decides whether to run
// episode e + 2, and all threads stop after the same episode.
constexpr long kMinEpisodes = 20;
inline void stop_if_time_is_up(Phase& ph, long e) {
    if (!ph.time_up.load(std::memory_order_relaxed)) return;
    const long last = (e + 1 > kMinEpisodes) ? e + 1 : kMinEpisodes;
    if (last < ph.final_episode.load()) ph.final_episode.store(last);
}

// ---- EPISODE CHECK: the code of thread t -------------------------------------------------------
template <class B>
void episode_body(Phase& ph, B& barrier, int t, bool jitter) {
    std::uint32_t rng = 0x9E3779B9u * static_cast<std::uint32_t>(t + 1) | 1u;
    for (long e = 1; e <= ph.final_episode.load(); ++e) {
        ph.episode[static_cast<std::size_t>(t)].v.store(e, std::memory_order_relaxed);
        if (jitter) random_delay(rng);

        barrier.wait(t);

        for (int i = 0; i < ph.threads; ++i) {
            const long seen = ph.episode[static_cast<std::size_t>(i)].v.load(std::memory_order_relaxed);
            if (seen < e)
                die(kExitFail, "FAIL " + ph.variant + " [" + ph.name + "]: WRONG EPISODE, thread " +
                               std::to_string(t) + " passed barrier " + std::to_string(e) +
                               " EARLY: thread " + std::to_string(i) + " is still in episode " +
                               std::to_string(seen));
            if (seen > e + 1)
                die(kExitFail, "FAIL " + ph.variant + " [" + ph.name + "]: WRONG EPISODE, thread " +
                               std::to_string(i) + " is in episode " + std::to_string(seen) +
                               " but thread " + std::to_string(t) + " has only just passed barrier " +
                               std::to_string(e));
        }
        if (t == 0) stop_if_time_is_up(ph, e);
    }
}

// ---- HANDOFF CHECK: the code of thread t -------------------------------------------------------
// data[] is deliberately not atomic (see the comment at the top of the file).
template <class B>
void handoff_body(Phase& ph, B& a, B& b, std::vector<PlainSlot>& data, int t) {
    for (long r = 1; r <= ph.final_episode.load(); ++r) {
        data[static_cast<std::size_t>(t)].v = r;                  // written before barrier a

        a.wait(t);

        for (int i = 0; i < ph.threads; ++i) {                    // read after barrier a
            const long seen = data[static_cast<std::size_t>(i)].v;
            if (seen != r)
                die(kExitFail, "FAIL " + ph.variant + " [" + ph.name + "]: WRONG VALUE, thread " +
                               std::to_string(t) + " passed barrier a of round " + std::to_string(r) +
                               " and read " + std::to_string(seen) + " from thread " + std::to_string(i));
        }
        ph.episode[static_cast<std::size_t>(t)].v.store(r, std::memory_order_relaxed);   // progress
        if (t == 0) stop_if_time_is_up(ph, r);

        b.wait(t);                                                // nobody writes before all have read
    }
}

// Runs body(t) for t = 0 .. threads-1, each in its own thread, and returns when all are done.
// The threads are not started together on purpose: the first threads reach the barrier while
// the others do not exist yet.
template <class Body>
void launch(bool use_openmp, int threads, Body body) {
#ifdef _OPENMP
    if (use_openmp) {
        // The OpenMP barrier only works for the threads of an OpenMP team.
        omp_set_dynamic(0);                                       // give me exactly `threads` threads
        int team = 0;
        #pragma omp parallel num_threads(threads)
        {
            #pragma omp single
            team = omp_get_num_threads();
            // "single" ends with an implicit barrier, so every thread sees the value of team.
            if (team == threads) body(omp_get_thread_num());
        }
        if (team != threads)
            die(kExitFail, "FAIL: OpenMP gave " + std::to_string(team) + " threads instead of " +
                           std::to_string(threads));
        return;
    }
#endif
    (void)use_openmp;
    std::vector<std::thread> pool;
    pool.reserve(static_cast<std::size_t>(threads));
    for (int t = 0; t < threads; ++t) pool.emplace_back([&body, t] { body(t); });
    for (auto& th : pool) th.join();
}

enum class Check { episode, handoff };

// One phase: `threads` threads run `target` episodes (fewer if the time budget is used up first).
// Returns the number of episodes completed. Does not return if a check fails or the test hangs.
template <class B>
long run_phase(const char* variant, const char* name, Check check, int threads, long target,
               bool jitter, bool use_openmp) {
    if (target < kMinEpisodes) target = kMinEpisodes;
    Phase ph(variant, name, threads, target);
    B a(threads);
    B b(threads);                                                 // second barrier (handoff check)
    std::vector<PlainSlot> data(static_cast<std::size_t>(threads));

    std::thread dog(watchdog, std::ref(ph));
    const double start = pc::now_s();
    launch(use_openmp, threads, [&](int t) {
        if (check == Check::episode) episode_body(ph, a, t, jitter);
        else                         handoff_body(ph, a, b, data, t);
    });
    const double seconds = pc::now_s() - start;
    ph.finished.store(true);
    dog.join();

    // Every thread must have completed exactly the same number of episodes.
    const long done = ph.final_episode.load();
    for (int t = 0; t < threads; ++t) {
        const long reached = ph.episode[static_cast<std::size_t>(t)].v.load();
        if (reached != done)
            die(kExitFail, std::string("FAIL ") + variant + " [" + name + "]: thread " + std::to_string(t) +
                           " stopped after episode " + std::to_string(reached) + ", expected " +
                           std::to_string(done));
    }
    std::printf("  %-22s %-10s threads=%-3d episodes=%-7ld %6.2f s%s\n", variant, name, threads, done,
                seconds, done < target ? "  (time budget reached)" : "");
    std::fflush(stdout);
    return done;
}

long scaled(double episodes) {
    return static_cast<long>(episodes * g_size);
}

// All phases for one barrier type.
//   speed          1.0 for barriers that wait in user space; smaller for barriers whose wait is
//                  a system call (an episode then costs tens of microseconds)
//   oversub_target episodes of the oversubscribed phase; small for pure spinning, where one
//                  episode can cost a whole scheduler time slice
//   handoff        false skips the handoff check (see main: std::barrier under ThreadSanitizer)
template <class B>
void test_variant(const char* variant, double speed, double oversub_target, bool use_openmp = false,
                  bool handoff = true) {
    if (!g_only.empty() && g_only != variant) return;
    const int hw = static_cast<int>(pc::hw_threads());
    const int few = hw >= 4 ? 4 : 2;                              // a thread count without oversubscription
    long total = 0;
    total += run_phase<B>(variant, "single",    Check::episode, 1,      scaled(10000 * speed),  false, use_openmp);
    total += run_phase<B>(variant, "reuse",     Check::episode, few,    scaled(300000 * speed), false, use_openmp);
    total += run_phase<B>(variant, "all-cores", Check::episode, hw,     scaled(100000 * speed), false, use_openmp);
    total += run_phase<B>(variant, "jitter",    Check::episode, few,    scaled(50000 * speed),  true,  use_openmp);
    total += run_phase<B>(variant, "oversub",   Check::episode, 2 * hw, scaled(oversub_target), true,  use_openmp);
    if (handoff)
        total += run_phase<B>(variant, "handoff", Check::handoff, few,  scaled(100000 * speed), false, use_openmp);
    std::printf("PASS %s (%ld barrier episodes checked)\n", variant, total);
    std::fflush(stdout);
}

}  // namespace

int main(int argc, char** argv) {
    pc::Args args(argc, argv);
    const bool small = args.has("small");
#if defined(PC_BARRIER_FUZZ)
    const char* const mode = "schedule fuzzing";
    g_size = 0.1;                                                 // every test point costs time
    g_cap_s = 0.5;
#else
    const char* const mode = "plain";
#endif
    if (small) { g_size = 0.05; g_stall_s = 30.0; }               // ThreadSanitizer is 10-50x slower
    if (args.has("stall_s")) g_stall_s = std::atof(args.str("stall_s", "").c_str());
    if (args.has("cap_s")) g_cap_s = std::atof(args.str("cap_s", "").c_str());
    g_only = args.str("impl", "");

#if defined(PC_MUTANT)
    // ---- mutation build: test the one broken barrier -------------------------------------------
    if (args.has("name")) { std::printf("%s\n", mutant::kName); return 0; }
    std::printf("mutant %s (PC_MUTANT=%d, %s), %u hardware threads\n", mutant::kName, PC_MUTANT, mode,
                pc::hw_threads());
    test_variant<mutant::Barrier>(mutant::kName, 1.0, 300);
    std::printf("SURVIVED %s: no check failed\n", mutant::kName);
    return 0;
#else
    // ---- normal build: every barrier of barrier.hpp --------------------------------------------
    if (args.has("list")) {
        std::printf("sr\nsr_bo_spin\nsr_bo_yield\nsr_bo_sleep\nsr_block\nstd_barrier\ncondvar\n");
#ifdef PC_HAVE_PTHREAD_BARRIER
        std::printf("pthread_barrier\n");
#endif
#ifdef PC_HAVE_OMP_BARRIER
        std::printf("omp_barrier\n");
#endif
        return 0;
    }
    std::printf("barrier correctness test (%s): %u hardware threads, %s sizes, phase budget %.1f s, watchdog %.0f s\n",
                mode, pc::hw_threads(), small ? "small" : "full", g_cap_s, g_stall_s);

    // The sense-reversing barrier with its five ways of waiting.
    // (arguments: name, speed factor for the episode targets, episodes of the oversubscribed phase)
    test_variant<pc::SenseBarrier<pc::NoBackoff>>("sr", 1.0, 300);                 // pure spinning
    test_variant<pc::SenseBarrier<pc::SpinBackoff>>("sr_bo_spin", 1.0, 300);
    test_variant<pc::SenseBarrier<pc::YieldBackoff>>("sr_bo_yield", 1.0, 20000);
    test_variant<pc::SenseBarrier<pc::SleepBackoff>>("sr_bo_sleep", 0.1, 5000);
    test_variant<pc::SenseBarrierBlocking>("sr_block", 0.3, 20000);

#if !defined(PC_BARRIER_FUZZ)       // the baselines have no test points: nothing to fuzz
    // The baselines go through the same checks: this validates the test (a correct barrier must
    // pass) and gives the same guarantee for the barriers used in the comparison.
    //
    // ThreadSanitizer only understands synchronisation that it can see. When a barrier is
    // implemented inside a library that is not instrumented, ThreadSanitizer misses the ordering
    // the barrier creates and reports FALSE data races on data that the barrier protects:
    //   - std::barrier of libc++ (macOS): its implementation is inside the system libc++ library.
    //     Only the handoff check (plain variables ordered by the barrier alone) is affected, so
    //     only that check is skipped. libstdc++ implements std::barrier in the header: no problem.
    //   - the OpenMP barrier: inside the OpenMP run-time library, like the rest of the parallel
    //     region, so even the test's own launcher is reported. The variant is skipped.
    // The argument tsan_uninstrumented runs everything anyway, to show the false reports.
    bool std_handoff = true;
    bool omp_run = true;
#if defined(PC_TSAN)
    if (!args.has("tsan_uninstrumented")) {
        omp_run = false;
#if defined(_LIBCPP_VERSION)
        std_handoff = false;
        std::printf("note: ThreadSanitizer + libc++: handoff check skipped for std_barrier (false positives, see docs/part2.md)\n");
#endif
#if defined(PC_HAVE_OMP_BARRIER)
        std::printf("note: ThreadSanitizer: omp_barrier skipped (OpenMP run-time not instrumented, see docs/part2.md)\n");
#endif
    }
#endif
    test_variant<pc::StdBarrier>("std_barrier", 0.3, 20000, /*use_openmp=*/false, std_handoff);
    test_variant<pc::CondVarBarrier>("condvar", 0.1, 5000);
#ifdef PC_HAVE_PTHREAD_BARRIER
    test_variant<pc::PthreadBarrier>("pthread_barrier", 0.1, 5000);
#endif
#ifdef PC_HAVE_OMP_BARRIER
    if (omp_run) test_variant<pc::OmpBarrier>("omp_barrier", 0.3, 5000, /*use_openmp=*/true);
#else
    (void)omp_run;
#endif
#endif  // !PC_BARRIER_FUZZ
    std::printf("ALL PASS\n");
    return 0;
#endif
}
