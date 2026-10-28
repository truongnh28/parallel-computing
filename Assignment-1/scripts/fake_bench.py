#!/usr/bin/env python3
"""fake_bench.py -- TEST DOUBLE for build/bench_lock, build/bench_barrier, build/bench_counter
and build/sleep_latency.

It honours the same command-line contract as the real programs (key=value arguments, `list`,
`header`, exactly one CSV row per run in the format of pc::csv_header()) and prints SYNTHETIC
numbers from a toy model. It exists only so that scripts/run_bench.py and scripts/plot.py can be
tested end to end without the real binaries and without loading the machine.

Every row it prints has a host name that starts with "FAKE". run_bench.py refuses to store such
rows under results/, and plot.py stamps "SYNTHETIC DATA" on every figure drawn from them.
Nothing printed by this script is a measurement.

Usage:
    fake_bench.py lock    lock=<name> threads=<T> iters=<I> outside_work=<OW> n=<N>
    fake_bench.py barrier impl=<name> threads=<T> iters=<episodes per thread>
    fake_bench.py counter impl=<name> threads=<T> iters=<I> outside_work=<OW> n=<N>
    fake_bench.py lock_yield ...                 (stands for build/bench_lock_yield)
    fake_bench.py <part> list | header
    fake_bench.py sleep_latency

Environment (all optional):
    FAKE_HOST      suffix of the host name (host = "FAKE-<suffix>", default "FAKE")
    FAKE_NCPU      number of cores of the pretended machine (default 10)
    FAKE_PCORES    number of performance cores (default 4; 0 = homogeneous machine)
    FAKE_ARCH      arm64 (default) or x86_64
    FAKE_REALTIME  if set to a float s > 0, really sleep s * (synthetic seconds) before printing,
                   so that the runner's timeout path can be exercised
    FAKE_FAIL      comma list of implementation names whose rows report ok=0
"""
import math
import os
import random
import sys
import time

HEADER = ("host,arch,os,compiler,ncpu,part,impl,threads,iters,outside_work,n,"
          "seconds,ops,ops_per_s,ok,load1,extra")

NCPU = int(os.environ.get("FAKE_NCPU", "10"))
PCORES = int(os.environ.get("FAKE_PCORES", "4"))
ARCH = os.environ.get("FAKE_ARCH", "arm64")
HOST = "FAKE" + ("-" + os.environ["FAKE_HOST"] if os.environ.get("FAKE_HOST") else "")
SPEED = 1.0 if ARCH == "arm64" else 1.6          # the pretended x86 machine is slower per core

LOCKS = ["tas", "ttas", "mcs",
         "tas_bo_sleep", "tas_bo_spin", "tas_bo_yield",
         "ttas_bo_sleep", "ttas_bo_spin", "ttas_bo_yield",
         "std_mutex", "pthread_mutex"] + \
        (["os_unfair_lock"] if ARCH == "arm64" else ["pthread_spinlock", "omp_lock"])
BARRIERS = ["sr", "sr_bo_spin", "sr_bo_yield", "sr_bo_sleep", "sr_block", "std_barrier", "condvar"] + \
           ([] if ARCH == "arm64" else ["pthread_barrier", "omp_barrier"])
COUNTERS = ["faa", "faa_seqcst", "cas", "cas_seqcst", "cas_spin", "cas_yield", "cas_sleep"] + \
           ["lock_" + name for name in LOCKS]


def parse(argv):
    kv = {}
    for a in argv:
        if "=" in a:
            k, v = a.split("=", 1)
            kv[k] = v
        else:
            kv[a] = "1"
    return kv


def noise():
    """Multiplicative noise: 3 % log-normal, plus a rare slow outlier (background activity)."""
    x = math.exp(random.gauss(0.0, 0.03))
    if random.random() < 0.04:
        x *= 1.0 + random.random() * 0.5
    return x


def core_speed(p):
    """Mean slowdown of a thread when p threads share P fast cores and NCPU - P slow ones."""
    if PCORES <= 0 or p <= PCORES:
        return 1.0
    slow = min(p, NCPU) - PCORES
    return (PCORES + slow * 1.7) / (PCORES + slow)


def emit(part, impl, threads, iters, ow, n, seconds, ops, extra):
    ok = 0 if impl in os.environ.get("FAKE_FAIL", "").split(",") else 1
    scale = float(os.environ.get("FAKE_REALTIME", "0") or 0)
    if scale > 0:
        time.sleep(seconds * scale)
    rate = ops / seconds if seconds > 0 else 0.0
    compiler = "clang-17.0" if ARCH == "arm64" else "gcc-13.2"
    osname = "macOS" if ARCH == "arm64" else "Linux"
    print("%s,%s,%s,%s,%d,%s,%s,%d,%d,%d,%d,%.9f,%.0f,%.3f,%d,%.2f,%s" % (
        HOST, ARCH, osname, compiler, NCPU, part, impl, threads, iters, ow, n,
        seconds, ops, rate, ok, 3.0 + random.random(), extra))


def t_prime(n):
    return SPEED * (1.0e-9 * max(n - 2, 0) + 4.0e-9)       # one is_prime(n) call


def run_lock(kv):
    impl, p = kv.get("lock", "tas"), int(kv.get("threads", 1))
    iters, ow, n = int(kv.get("iters", 1000)), int(kv.get("outside_work", 0)), int(kv.get("n", 1009))
    if impl not in LOCKS:
        sys.exit("unknown lock: " + impl)
    t = t_prime(n) * core_speed(p)
    uncontended = {"std_mutex": 18e-9, "pthread_mutex": 20e-9, "mcs": 12e-9}.get(impl, 6e-9)
    contenders = max(0.0, p - (1 + ow))                     # threads queued when saturated
    if impl.startswith("tas"):
        handoff = 60e-9 * contenders
    elif impl.startswith("ttas"):
        handoff = 25e-9 * contenders
    elif impl == "mcs":
        handoff = 45e-9 * min(contenders, 1.0)
    elif impl in ("pthread_spinlock", "omp_lock"):
        handoff = 40e-9 * contenders
    else:
        handoff = 90e-9 * min(contenders, 1.0)
    if "bo_spin" in impl or "bo_yield" in impl:
        handoff *= 0.45
    if "bo_sleep" in impl:
        handoff = handoff * 0.2 + (8e-6 if contenders > 0 else 0.0)   # the sleep is never short
    cs = t + uncontended + handoff
    seconds = iters * max(p * cs, (1 + ow) * t + uncontended)
    if p > NCPU:                                            # oversubscription
        over = p / NCPU
        if impl == "mcs":
            seconds *= 1.0 + 60.0 * (over - 1.0)            # FIFO hand-off to a preempted thread
        elif impl in ("tas", "ttas", "pthread_spinlock") or "bo_spin" in impl:
            seconds *= 1.0 + 2.5 * (over - 1.0)
        else:
            seconds *= 1.0 + 0.15 * (over - 1.0)
    relax = kv.get("_relax") or ("isb" if ARCH == "arm64" else "pause")
    if relax == "yield" and p > 1:
        seconds *= 1.12 if impl in ("tas", "ttas") else 0.97
    seconds *= noise()
    # per-thread spread of the finishing times: small for the FIFO lock, large for TAS
    spread = {"mcs": 0.02}.get(impl, 0.35 if impl.startswith("tas") else 0.15)
    spread *= (0.0 if p == 1 else 1.0) * (0.7 + 0.6 * random.random())
    extra = "thr_min_s:%.6f;thr_max_s:%.6f;relax:%s" % (
        seconds * (1.0 - spread), seconds * 0.999, relax)
    emit("lock", impl, p, iters, ow, n, seconds, p * iters, extra)


def run_barrier(kv):
    impl, p, iters = kv.get("impl", "sr"), int(kv.get("threads", 1)), int(kv.get("iters", 1000))
    if impl not in BARRIERS:
        sys.exit("unknown barrier: " + impl)
    if impl in ("sr", "sr_bo_spin", "sr_bo_yield"):
        cost = 15e-9 + 55e-9 * (p - 1) * core_speed(p)
        if impl == "sr_bo_spin":
            cost *= 1.3
        if p > NCPU:
            cost += (4e-3 if impl != "sr_bo_yield" else 3e-5) * (p - NCPU)
    elif impl == "sr_bo_sleep":
        cost = 15e-9 + (12e-6 if p > 1 else 0.0) + 0.4e-6 * p
    else:                                                   # blocking variants
        base = {"sr_block": 2.5e-6, "std_barrier": 4e-6, "condvar": 6e-6,
                "pthread_barrier": 5e-6, "omp_barrier": 1.5e-6}[impl]
        cost = 25e-9 + (base * (p - 1) if p > 1 else 0.0)
    seconds = iters * cost * SPEED * noise()
    emit("barrier", impl, p, iters, 0, 0, seconds, iters, "")          # ops = episodes


def run_counter(kv):
    impl, p = kv.get("impl", "fetch_add"), int(kv.get("threads", 1))
    iters, ow, n = int(kv.get("iters", 1000)), int(kv.get("outside_work", 0)), int(kv.get("n", 101))
    if impl not in COUNTERS:
        sys.exit("unknown counter: " + impl)
    t = t_prime(n) * core_speed(p)
    if impl.startswith("lock_"):
        lock = impl[5:]
        base = {"std_mutex": 18e-9, "pthread_mutex": 20e-9, "mcs": 12e-9}.get(lock, 6e-9)
        bounce = 70e-9 if lock.startswith("tas") else 45e-9 if lock.startswith("ttas") else 65e-9
        if "bo_spin" in lock or "bo_yield" in lock:
            bounce *= 0.5
        if "bo_sleep" in lock:
            bounce = 4e-6
    else:
        base = 2e-9 if impl.startswith("faa") else 3e-9
        bounce = {"faa": 28e-9, "faa_seqcst": 30e-9, "cas": 34e-9, "cas_seqcst": 36e-9,
                  "cas_spin": 22e-9, "cas_yield": 20e-9, "cas_sleep": 10e-9}[impl]
    share = base / (base + ow * t) if ow else 1.0           # fraction of time at the counter
    load = min(1.0, (p - 1) * share)                         # how contended the line is
    inc = base + bounce * load
    fail_per_inc = 0.0
    if impl.startswith("cas"):
        fail_per_inc = {"cas": 0.9, "cas_seqcst": 0.95, "cas_spin": 0.35, "cas_yield": 0.3,
                        "cas_sleep": 0.05}[impl] * load * math.log2(p + 1)
        inc += fail_per_inc * 12e-9
        if impl == "cas_sleep":
            inc += fail_per_inc * 9e-6
    seconds = iters * max(p * inc * load + (1 - load) * (inc + ow * t), inc + ow * t)
    if p > NCPU and impl in ("lock_mcs", "lock_tas", "lock_ttas", "lock_tas_bo_spin", "lock_ttas_bo_spin"):
        seconds *= 1.0 + (40.0 if impl == "lock_mcs" else 2.0) * (p / NCPU - 1.0)
    seconds *= noise()
    ops = p * iters
    thr = "thr_min_s:%.6f;thr_max_s:%.6f" % (seconds * (0.8 + 0.15 * random.random()), seconds * 0.999)
    extra = ("cas_fail:%d;" % int(fail_per_inc * ops * noise()) if impl.startswith("cas") else "") + thr
    emit("counter", impl, p, iters, ow, n, seconds, ops, extra)


def run_sleep_latency():
    osname = "macOS" if ARCH == "arm64" else "Linux"
    print("host,os,arch,what,requested_ns,samples,min_ns,median_ns,p99_ns,max_ns")
    row = "%s,%s,%s" % (HOST, osname, ARCH) + ",%s,%d,%d,%.1f,%.1f,%.1f,%.1f"
    print(row % ("clock", 0, 20000, 0.0, 0.0, 42.0, 84.0))
    for req in (1, 10, 100, 1000, 10000, 100000, 1000000):
        med = (2600 if ARCH == "arm64" else 52000) + req * 1.25 + random.random() * 300
        print(row % ("sleep_for", req, 2000, med * 0.8, med, med * 4, med * 6))
    print(row % ("yield", 0, 20000, 41.0, 125.0, 250.0, 9834.0))
    print(row % ("cpu_relax", 0, 2000, 7.4, 7.6, 10.0, 15.1))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    part, kv = sys.argv[1], parse(sys.argv[2:])
    if part == "sleep_latency":
        return run_sleep_latency()
    names = {"lock": LOCKS, "lock_yield": LOCKS, "barrier": BARRIERS, "counter": COUNTERS}.get(part)
    if names is None:
        sys.exit("unknown part: " + part)
    if "list" in kv:
        print("\n".join(names))
    elif "header" in kv:
        print(HEADER)
    else:
        if part == "lock_yield":                           # the build with the other spin-wait hint
            kv["_relax"] = "yield"
            part = "lock"
        {"lock": run_lock, "barrier": run_barrier, "counter": run_counter}[part](kv)


if __name__ == "__main__":
    main()
