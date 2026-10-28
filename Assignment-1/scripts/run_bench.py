#!/usr/bin/env python3
"""run_bench.py -- runs the whole measurement campaign of Assignment 1 and stores the raw rows.

    python3 scripts/run_bench.py                 full campaign  -> results/<host>/
    python3 scripts/run_bench.py --dry-run       print the grid and the estimated duration only
    python3 scripts/run_bench.py --quick         tiny grid, 2 repetitions -> build/quick-results/<host>/
    python3 scripts/run_bench.py --part lock     only one part (repeatable)
    python3 scripts/run_bench.py --resume        continue an interrupted campaign
    python3 scripts/run_bench.py --resume --relax-yield --part lock_yield
                                                 optional extra sweep (make bench-relax)

What it writes into results/<host>/ (host = the `host` field printed by the benchmark programs):
    lock.csv, barrier.csv, counter.csv   one raw row per run: the columns of pc::csv_header()
                                         plus rep, timestamp, wall_s
    lock_yield.csv                       the same for build/bench_lock_yield (AArch64 only, optional)
    calibration.json                     the iteration counts chosen for this machine
    machine.txt                          output of scripts/machine_info.sh
    sleep_latency.csv                    output of build/sleep_latency
    campaign.log                         start/end time, load average, number of timeouts

Rules built into the runner (see README.md, "Measurement rules"):
  * one benchmark process at a time;
  * repetitions are the OUTER loop and the configurations (of all parts) are shuffled inside each
    repetition with a fixed seed, so slow drift of the background load or of the temperature
    cannot favour one implementation;
  * the campaign does not start when the 1-minute load average is above --load-max;
  * every run has a timeout; a run that exceeds it is recorded as ok=0, extra=timeout. The
    configuration is then repeated with REDUCE_FACTOR times fewer iterations, so that a collapse
    (an oversubscribed queue lock can be hundreds of times slower) becomes a measured number;
    plot.py compares runs through their time per iteration. A configuration that timed out
    SKIP_AFTER_TIMEOUTS times is not run again (ok=0, extra=timeout_skipped);
  * nothing is filtered or averaged here: plot.py reads the raw rows.

Python 3 standard library only; works on macOS and Linux.
"""
import argparse
import datetime
import fnmatch
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time

# =================================================================================================
# THE GRID -- everything that defines the campaign is in this block.
# =================================================================================================
REPS = 7                    # repetitions of every configuration (median, min, max are reported)
SEED = 20261031             # seed of the shuffle; repetition r uses SEED + r
LOAD_MAX = 7.0              # refuse to start above this 1-minute load average
BUDGET_MIN = 70.0           # the targets below are scaled down if the estimate exceeds this
BUDGET_RESERVE = 0.10       # part of the budget kept for timeouts and for runs above the model

# Thread counts. Adapted to the machine by machine_threads(): values above 2 x ncpu are dropped;
# ncpu, 2 x ncpu and one value in between are always present (oversubscription is a result, not
# a measurement error); the number of performance/physical cores is added when it is known.
THREADS = [1, 2, 3, 4, 6, 8, 10, 12, 16, 20]

# N must be prime (a composite N makes is_prime return at the first divisor).
N_SHORT, N_MEDIUM, N_LONG = 11, 1009, 10007      # critical section: ~10 ns, ~1 us, ~10 us
N_COUNTER = 101                                  # work unit between two increments: ~0.1 us

# Each entry is one sweep. A configuration that appears in two sweeps is run once (the first
# sweep wins).
#   part          which program runs it and which CSV file gets the rows (see PROGRAMS below)
#   impls         "*" or a list of names / glob patterns (names are discovered with `list`)
#   threads       "all" = machine_threads(); "few" = [1, P-cores (or ncpu/2), ncpu, 2*ncpu];
#                 or an explicit list
#   iters         how NUM_OF_ITERS is chosen:
#     ("per_bound", T, p)  locks. A lock program with p threads and I iterations cannot finish
#                          before I * t * max(p, 1 + OW) (t = one is_prime call), so one I per N
#                          would make the runs up to 20 times longer at 20 threads. Instead
#                              I = W_N / max(threads, 1 + OUTSIDE_WORK),
#                          which puts that lower bound at about T seconds for every configuration.
#                          W_N is calibrated once per N: a run of the reference lock with p
#                          threads and OUTSIDE_WORK = 0 is timed and scaled to T seconds. p = 1
#                          measures t itself; for the short critical section p = 4 is used,
#                          because there the cost is the lock hand-off, not is_prime (and the
#                          1-thread runs, which have no hand-off, get ONE_THREAD_BOOST x I);
#     ("per_config", T)    one value per (implementation, threads, OUTSIDE_WORK), each from a
#                          short pilot run scaled to about T seconds -- where the cost per
#                          operation varies by orders of magnitude between configurations
#                          (barrier: 5 ns to milliseconds per episode; counter: 2 ns to 2 ms
#                          per increment);
#     an integer           fixed.
#                 The iteration count is fixed for the whole campaign (calibration.json) and
#                 written in every row. Because it differs between configurations, plot.py
#                 compares runs through their time per iteration.
#   ref           preference list for the reference lock of a per_bound calibration.
GRID = [
    # Part 1, main sweep: every lock x every thread count x every contention level, at the
    # medium critical section. Figures L1, L2, L4, L5.
    dict(part="lock", sweep="main", impls="*", threads="all",
         outside_work=[0, 1, 3, 7, 15], n=[N_MEDIUM],
         iters=("per_bound", 0.19, 1), ref=["ttas", "tas"]),
    # Part 1, short critical section: the cost is the lock hand-off, so the locks differ most
    # and backoff matters most.
    dict(part="lock", sweep="short_cs", impls="*", threads="all",
         outside_work=[0, 3], n=[N_SHORT],
         iters=("per_bound", 0.19, 4), ref=["ttas", "tas"]),
    # Part 1, long critical section (with the two sweeps above: figure L3).
    dict(part="lock", sweep="long_cs", impls="*", threads="all",
         outside_work=[0, 3], n=[N_LONG],
         iters=("per_bound", 0.19, 1), ref=["ttas", "tas"]),
    # Part 1, optional (--relax-yield): the same benchmark built with -DPC_RELAX_YIELD (AArch64:
    # YIELD instead of ISB as the spin-wait hint, see docs/part1.md). Same iteration counts as
    # "main". Needs `make build/bench_lock_yield`.
    dict(part="lock_yield", sweep="relax", impls=["tas", "ttas"], threads="few",
         outside_work=[0, 3], n=[N_MEDIUM],
         iters=("per_bound", 0.19, 1), ref=["ttas", "tas"]),
    # Part 2: episodes per second, no work between two barriers. Figure B1.
    dict(part="barrier", sweep="episodes", impls="*", threads="all",
         outside_work=[0], n=[0],
         iters=("per_config", 0.25)),
    # Part 3: execution time of threads x iters increments. Figures C1, C2.
    dict(part="counter", sweep="main", impls="*", threads="all",
         outside_work=[0, 1, 8], n=[N_COUNTER],
         iters=("per_config", 0.15)),
]

# Known collapse: a queue lock hands the lock to one particular thread, and when there are more
# threads than cores that thread is often not running, so one hand-over costs about a scheduling
# quantum (about 1 400 hand-overs per second were observed with 20 threads on 10 cores). These
# configurations get DIVIDE times fewer iterations from the start, so that the collapse is a
# measured number and not only a timeout. (per_config sweeps need no such rule: their pilot run
# sees the collapse.) Any other configuration that times out is handled the same way at run time.
#                 part glob, implementation glob, condition, divide
REDUCED_ITERS = [("lock*", "mcs", "oversubscribed", 100)]
ONE_THREAD_BOOST = 8        # see per_bound above

# --quick: a few seconds per part, only to check that the pipeline works. Never used for the report.
QUICK_REPS = 2
QUICK_GRID = [
    dict(part="lock", sweep="main", impls="*", threads="quick",
         outside_work=[0, 3], n=[N_MEDIUM], iters=("per_bound", 0.02, 1), ref=["ttas", "tas"]),
    dict(part="lock", sweep="short_cs", impls="*", threads="quick",
         outside_work=[0], n=[N_SHORT], iters=("per_bound", 0.02, 2), ref=["ttas", "tas"]),
    dict(part="lock_yield", sweep="relax", impls=["tas", "ttas"], threads="quick",
         outside_work=[0], n=[N_MEDIUM], iters=("per_bound", 0.02, 1), ref=["ttas", "tas"]),
    dict(part="barrier", sweep="episodes", impls="*", threads="quick",
         outside_work=[0], n=[0], iters=("per_config", 0.02)),
    dict(part="counter", sweep="main", impls="*", threads="quick",
         outside_work=[0, 8], n=[N_COUNTER], iters=("per_config", 0.02)),
]

# Timeout of one run, by kind of program (--timeout lowers all of them), and the exceptions.
# A run that exceeds its timeout is a data row (ok=0, extra=timeout), not an error.
TIMEOUT_S = {"lock": 30.0, "barrier": 10.0, "counter": 10.0}
#                  part glob, implementation glob, condition, seconds
LONG_TIMEOUTS = [("barrier", "sr", "oversubscribed", 30.0),          # one episode can cost a
                 ("barrier", "sr_bo_spin", "oversubscribed", 30.0)]  # whole scheduler time slice
# Limits of a per_config iteration count: kind -> (smallest, largest).
PILOT_LIMITS = {"barrier": (50, 50_000_000), "counter": (100, 2_000_000_000)}
REDUCED_EST_S = 3.0         # a run of a REDUCED_ITERS configuration, for the estimate only
SKIP_AFTER_TIMEOUTS = 2     # a configuration that timed out this often is not run again
REDUCE_FACTOR = 100         # after a timeout: repeat with this many times fewer iterations
MIN_ITERS = 10              # ... but never fewer than this
CALIB_TIMEOUT_S = 10.0
OVERHEAD_S = 0.02           # process start + thread creation, for the estimate only
LOCK_SLACK = 1.5            # measured lock runs / lower bound, for the estimate only
MIN_SCALE = 0.25            # the budget never shrinks a target below this fraction
# =================================================================================================

HEADER = ("host,arch,os,compiler,ncpu,part,impl,threads,iters,outside_work,n,"
          "seconds,ops,ops_per_s,ok,load1,extra")
NFIELDS = HEADER.count(",") + 1
EXTRA_COLUMNS = "rep,timestamp,wall_s"
# part -> (program in build/, kind). The kind decides the arguments and the cost model; the part
# names the CSV file. "lock_yield" is optional (built by `make build/bench_lock_yield`).
PROGRAMS = {"lock": ("bench_lock", "lock"), "barrier": ("bench_barrier", "barrier"),
            "counter": ("bench_counter", "counter"), "lock_yield": ("bench_lock_yield", "lock")}
PARTS = list(PROGRAMS)
OPTIONAL_PARTS = ["lock_yield"]
IMPL_KEY = {"lock": "lock", "barrier": "impl", "counter": "impl"}


def kind_of(part):
    return PROGRAMS[part][1]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_DIR = os.path.join(ROOT, "results")


def log(msg=""):
    print(msg, flush=True)


def die(msg, code=2):
    print("run_bench: " + msg, file=sys.stderr, flush=True)
    sys.exit(code)


def fmt_dur(s):
    s = int(round(max(0.0, s)))
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def load1():
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return -1.0


def is_prime(n):
    return n >= 2 and all(n % d for d in range(2, int(n ** 0.5) + 1))


# ---- the machine --------------------------------------------------------------------------------
def mid_core_count(ncpu):
    """Number of performance cores (Apple Silicon) or of physical cores when SMT is on (Linux);
    None when it is unknown or equal to ncpu."""
    mid = None
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            mid = int(out)
        elif os.path.exists("/proc/cpuinfo"):
            cores, phys, core = set(), None, None
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("physical id"):
                        phys = line.split(":")[1].strip()
                    elif line.startswith("core id"):
                        core = line.split(":")[1].strip()
                    elif not line.strip():
                        if phys is not None and core is not None:
                            cores.add((phys, core))
                        phys = core = None
            mid = len(cores) or None
    except (OSError, ValueError, subprocess.SubprocessError):
        mid = None
    return mid if mid and 1 < mid < ncpu else None


def machine_threads(ncpu, mid):
    ts = {t for t in THREADS if t <= 2 * ncpu} | {1, ncpu, 2 * ncpu}
    if mid:
        ts.add(mid)
    if not any(ncpu < t < 2 * ncpu for t in ts) and ncpu > 1:
        ts.add(ncpu + max(1, ncpu // 2))
    if ncpu > 2 * max(THREADS):                       # large server: fill the gap below ncpu
        ts.add(ncpu // 2)
    return sorted(ts)


def resolve_threads(spec, ncpu, mid):
    if spec == "all":
        return machine_threads(ncpu, mid)
    if spec == "few":
        return sorted({1, mid or max(2, ncpu // 2), ncpu, 2 * ncpu})
    if spec == "quick":
        return sorted({1, 2, min(4, ncpu), ncpu + 2})
    return sorted(set(int(t) for t in spec))


# ---- running one benchmark process ----------------------------------------------------------------
class Runner:
    def __init__(self, bin_dir, fake, env):
        self.env = env
        self.cmd = {}
        fake_py = os.path.join(ROOT, "scripts", "fake_bench.py")
        for part in PARTS + ["sleep_latency"]:
            if fake:
                self.cmd[part] = [sys.executable, fake_py, part]
            else:
                exe = os.path.join(bin_dir, part if part == "sleep_latency" else PROGRAMS[part][0])
                if os.path.isfile(exe) and os.access(exe, os.X_OK):
                    self.cmd[part] = [exe]
        self.child = None

    def has(self, part):
        return part in self.cmd

    def simple(self, part, *args, timeout=60.0):
        """Run `<program> args` and return its stdout (used for list, header, sleep_latency)."""
        out = subprocess.run(self.cmd[part] + list(args), capture_output=True, text=True,
                             timeout=timeout, env=self.env)
        if out.returncode != 0:
            raise RuntimeError("%s %s failed (rc=%d): %s" % (
                self.cmd[part][-1], " ".join(args), out.returncode, out.stderr.strip()[:300]))
        return out.stdout

    def args_for(self, part, impl, threads, iters, ow, n):
        a = ["%s=%s" % (IMPL_KEY[kind_of(part)], impl), "threads=%d" % threads, "iters=%d" % iters]
        if kind_of(part) != "barrier":
            a += ["outside_work=%d" % ow, "n=%d" % n]
        return a

    def run(self, part, impl, threads, iters, ow, n, timeout):
        """One measurement. Returns (status, fields, wall_s, message); status is "ok", "timeout"
        or "error"; fields is the list of the NFIELDS CSV fields when status == "ok"."""
        cmd = self.cmd[part] + self.args_for(part, impl, threads, iters, ow, n)
        t0 = time.monotonic()
        # Own session: on a timeout the whole process group is killed, so no spinning thread
        # survives and disturbs the following runs.
        self.child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, env=self.env, start_new_session=True)
        try:
            out, err = self.child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            return "timeout", None, time.monotonic() - t0, "timeout after %.1f s" % timeout
        finally:
            rc = self.child.returncode if self.child else None
            self.child = None
        wall = time.monotonic() - t0
        rows = [ln.strip() for ln in out.splitlines()
                if ln.count(",") >= NFIELDS - 1 and not ln.startswith("host,")]
        if rc != 0 or not rows:
            tail = (err.strip() or out.strip()).splitlines()[-1:] or [""]
            return "error", None, wall, "rc=%s %s" % (rc, tail[0][:200])
        fields = rows[-1].split(",", NFIELDS - 1)
        return "ok", fields, wall, ""

    def kill(self):
        if self.child is None:
            return
        try:
            os.killpg(self.child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.child.communicate(timeout=10)
        except Exception:                                  # noqa: BLE001 -- best effort
            pass


# ---- the grid -> list of configurations ------------------------------------------------------------
class Config:
    __slots__ = ("part", "sweep", "impl", "threads", "ow", "n", "policy", "ref", "ckey", "rkey", "ncpu")

    def __init__(self, part, sweep, impl, threads, ow, n, policy, ref):
        self.part, self.sweep, self.impl, self.threads = part, sweep, impl, threads
        self.ow, self.n, self.policy, self.ref = ow, n, policy, ref
        if isinstance(policy, int):
            self.ckey = None
        elif policy[0] == "per_config":
            self.ckey = "%s|%s|t%d|ow%d|n%d" % (part, impl, threads, ow, n)
        else:                          # W_N, shared by all programs of the same kind (lock, lock_yield)
            self.ckey = "%s|W|n%d" % (kind_of(part), n)
        self.rkey = "reduced|%s|%s|t%d|ow%d|n%d" % (part, impl, threads, ow, n)

    @property
    def key(self):                     # identity of a configuration (iters is not part of it)
        return (self.part, self.impl, self.threads, self.ow, self.n)

    def label(self):
        s = "%-10s %-19s p=%-3d" % (self.part, self.impl, self.threads)
        return s if kind_of(self.part) == "barrier" else s + " ow=%-2d n=%d" % (self.ow, self.n)


def divide_for(cfg, ncpu):
    """Proactive reduction of the iteration count (REDUCED_ITERS)."""
    for part_pat, impl_pat, cond, div in REDUCED_ITERS:
        if fnmatch.fnmatch(cfg.part, part_pat) and fnmatch.fnmatch(cfg.impl, impl_pat):
            if cond == "always" or (cond == "oversubscribed" and cfg.threads > ncpu):
                return div
    return 1


def bound_iters(cfg, entry, ncpu):
    """I = W_N / max(threads, 1 + OW) for a per_bound configuration."""
    it = entry["w"] / max(cfg.threads, 1 + cfg.ow)
    if cfg.threads == 1 and entry["at_threads"] > 1:
        it *= ONE_THREAD_BOOST
    return max(MIN_ITERS, int(round(it / divide_for(cfg, ncpu))))


def expand(grid, impls, ncpu, mid, impl_filter):
    configs, seen = [], set()
    for sw in grid:
        part = sw["part"]
        if part not in impls:
            continue
        for n in sw["n"]:
            if kind_of(part) != "barrier" and not is_prime(n):
                die("grid error: n=%d is not prime (sweep %s/%s)" % (n, part, sw["sweep"]))
        names = [i for i in impls[part]
                 if (sw["impls"] == "*" or any(fnmatch.fnmatch(i, pat) for pat in sw["impls"]))
                 and (not impl_filter or any(fnmatch.fnmatch(i, pat) for pat in impl_filter))]
        for impl in names:
            for t in resolve_threads(sw["threads"], ncpu, mid):
                for ow in sw["outside_work"]:
                    for n in sw["n"]:
                        c = Config(part, sw["sweep"], impl, t, ow, n, sw["iters"], sw.get("ref", []))
                        c.ncpu = ncpu
                        if c.key not in seen:
                            seen.add(c.key)
                            configs.append(c)
    return configs


# ---- calibration ------------------------------------------------------------------------------------
def calibrate_one(runner, part, impl, threads, ow, n, target, floor, ceil=4_000_000_000):
    """Find an iteration count for which this run lasts about `target` seconds: grow a short run
    until it is long enough to be timed, then scale linearly.
    Returns (iters, predicted seconds of a run with that count, note)."""
    aim = min(0.03, target)
    iters, last = (20 if kind_of(part) == "barrier" else 200), None
    for _ in range(16):
        status, f, _, msg = runner.run(part, impl, threads, iters, ow, n, CALIB_TIMEOUT_S)
        if status != "ok":
            if last:                                       # keep the largest size that worked
                return last[0], last[1], "calibration stopped: " + msg
            return iters, CALIB_TIMEOUT_S, "calibration failed: " + msg
        sec = float(f[11])
        last = (iters, sec)
        if sec >= aim / 3.0:
            best = min(ceil, max(floor, int(round(iters * target / sec))))
            return best, sec * best / iters, ""
        # Never more than x10 per step: where a configuration can collapse (queue lock with more
        # threads than cores) a bigger jump can turn a 10 ms pilot into one that hits the timeout.
        grow = 10.0 if sec <= 0 else min(10.0, max(3.0, aim / sec))
        iters = int(iters * grow)
        if iters > 4_000_000_000:
            break
    return last[0], last[1], "calibration did not converge"


def calibrate(runner, configs, impls, calib, scale, path, verbose):
    """Fill `calib` (ckey -> entry) for every configuration that has no entry yet."""
    todo = {}
    for c in configs:
        if c.ckey and c.ckey not in calib and c.ckey not in todo:
            todo[c.ckey] = c
    if not todo:
        return
    log("calibrating %d iteration counts (pilot runs) ..." % len(todo))
    t0 = last = time.monotonic()
    for i, (ckey, c) in enumerate(todo.items(), 1):
        target = c.policy[1] * scale
        if c.policy[0] == "per_config":
            impl, threads, ow = c.impl, c.threads, c.ow
            floor, ceil = PILOT_LIMITS[kind_of(c.part)]
            iters, sec, note = calibrate_one(runner, c.part, impl, threads, ow, c.n, target, floor, ceil)
            calib[ckey] = dict(iters=iters, target_s=round(target, 6), est_s=round(sec, 6), note=note)
        else:                                               # per_bound: W_N from one run at OUTSIDE_WORK = 0
            threads, ow = c.policy[2], 0
            impl = next((r for r in c.ref if r in impls[c.part]), impls[c.part][0])
            iters, sec, note = calibrate_one(runner, c.part, impl, threads, ow, c.n, target, 1)
            calib[ckey] = dict(w=iters * threads, target_s=round(target, 6), at_threads=threads,
                               ref_impl=impl, note=note)
        if verbose or note:
            log("  %-38s %s=%-11d (%s, %d threads, target %.3f s)%s" % (
                ckey, "iters" if "iters" in calib[ckey] else "W", calib[ckey].get("iters", calib[ckey].get("w")),
                impl, threads, target, "  ! " + note if note else ""))
        elif time.monotonic() - last >= 15.0:
            last = time.monotonic()
            log("  %d/%d" % (i, len(todo)))
        save_json(path, calib)
    log("calibration done in %s" % fmt_dur(time.monotonic() - t0))


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def iters_of(cfg, calib):
    if cfg.rkey in calib:                                  # reduced after a timeout
        return calib[cfg.rkey]["iters"]
    if isinstance(cfg.policy, int):
        return cfg.policy
    if cfg.policy[0] == "per_bound":
        return bound_iters(cfg, calib[cfg.ckey], cfg.ncpu)
    return calib[cfg.ckey]["iters"]


def estimate(cfg, calib, scale):
    """Model estimate (seconds) of one run of cfg, before it has ever been run."""
    if isinstance(cfg.policy, int):
        return 0.1 + OVERHEAD_S
    target = calib[cfg.ckey]["target_s"] if cfg.ckey in calib else cfg.policy[1] * scale
    if cfg.policy[0] == "per_bound":                       # the run sits LOCK_SLACK above its lower bound
        if divide_for(cfg, cfg.ncpu) > 1:
            return REDUCED_EST_S + OVERHEAD_S
        return target * LOCK_SLACK + OVERHEAD_S
    return calib.get(cfg.ckey, {}).get("est_s", target) + OVERHEAD_S


def timeout_of(cfg, limit):
    t = TIMEOUT_S[kind_of(cfg.part)]
    for part_pat, impl_pat, cond, seconds in LONG_TIMEOUTS:
        if fnmatch.fnmatch(cfg.part, part_pat) and fnmatch.fnmatch(cfg.impl, impl_pat):
            if cond == "always" or (cond == "oversubscribed" and cfg.threads > cfg.ncpu):
                t = seconds
    return min(t, limit) if limit else t


# ---- CSV files ----------------------------------------------------------------------------------------
def csv_path(out_dir, part):
    return os.path.join(out_dir, part + ".csv")


def read_done(out_dir, part):
    """(impl, threads, ow, n, rep) of every row already stored, and the timeouts per configuration."""
    done, timeouts = set(), {}
    path = csv_path(out_dir, part)
    if not os.path.exists(path):
        return done, timeouts
    with open(path) as f:
        cols = f.readline().strip().split(",")
        ix = {c: i for i, c in enumerate(cols)}
        for line in f:
            p = line.rstrip("\n").split(",")
            if len(p) < len(cols):
                continue
            try:
                barrier = kind_of(part) == "barrier"
                key = (part, p[ix["impl"]], int(p[ix["threads"]]),
                       0 if barrier else int(p[ix["outside_work"]]),
                       0 if barrier else int(p[ix["n"]]))
                done.add(key + (int(p[ix["rep"]]),))
            except (ValueError, KeyError):
                continue
            if p[ix["extra"]].startswith("timeout"):
                timeouts[key] = timeouts.get(key, 0) + 1
    return done, timeouts


def append_row(out_dir, part, fields, rep, wall):
    path = csv_path(out_dir, part)
    new = not os.path.exists(path)
    with open(path, "a") as f:
        if new:
            f.write(HEADER + "," + EXTRA_COLUMNS + "\n")
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        f.write(",".join(fields) + ",%d,%s,%.3f\n" % (rep, stamp, wall))


def failed_row(meta, cfg, iters, seconds, extra):
    """A row for a run that produced no result: ok=0, the reason in `extra`."""
    return meta + [kind_of(cfg.part), cfg.impl, str(cfg.threads), str(iters), str(cfg.ow), str(cfg.n),
                   "%.9f" % seconds, "0", "0.000", "0", "%.2f" % load1(),
                   re.sub(r"[,\s]+", "_", extra)]


# ---- main ---------------------------------------------------------------------------------------------
def parse_args():
    ap = argparse.ArgumentParser(description="Run the measurement campaign of Assignment 1.")
    ap.add_argument("--quick", action="store_true",
                    help="tiny grid, %d repetitions, into build/quick-results/ (pipeline test)" % QUICK_REPS)
    ap.add_argument("--part", action="append", choices=PARTS, help="only this part (repeatable)")
    ap.add_argument("--relax-yield", action="store_true",
                    help="also run the optional sweep with build/bench_lock_yield (make build/bench_lock_yield)")
    ap.add_argument("--impl", help="comma list of implementation names or glob patterns")
    ap.add_argument("--reps", type=int, help="number of repetitions (default %d)" % REPS)
    ap.add_argument("--out", help="output directory (default: results/)")
    ap.add_argument("--resume", action="store_true",
                    help="continue: skip the runs already stored, reuse calibration.json")
    ap.add_argument("--fresh", action="store_true",
                    help="start again: move the files of an earlier campaign into old-<time>/")
    ap.add_argument("--force", action="store_true", help="start even when the load is above --load-max")
    ap.add_argument("--load-max", type=float, default=LOAD_MAX,
                    help="largest 1-minute load average at the start (default %.1f)" % LOAD_MAX)
    ap.add_argument("--wait", type=float, default=0.0, metavar="MIN",
                    help="wait up to MIN minutes for the load to fall below --load-max")
    ap.add_argument("--timeout", type=float, default=0.0,
                    help="upper limit of the per-run timeout in seconds (default: %s)"
                    % ", ".join("%s %.0f" % kv for kv in TIMEOUT_S.items()))
    ap.add_argument("--budget", type=float, default=BUDGET_MIN, metavar="MIN",
                    help="shrink the run lengths so that the estimate fits (default %.0f; 0 = never)" % BUDGET_MIN)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--host", help="name of this machine in the results (sets PC_HOST)")
    ap.add_argument("--bin-dir", default=os.path.join(ROOT, "build"), help="directory of the benchmark programs")
    ap.add_argument("--fake", action="store_true",
                    help="use scripts/fake_bench.py instead of the real programs (testing; needs --out)")
    ap.add_argument("--dry-run", action="store_true", help="print the grid and the estimate, run nothing")
    ap.add_argument("-v", "--verbose", action="store_true", help="one line per run")
    return ap.parse_args()


def inside(path, parent):
    path, parent = os.path.realpath(path), os.path.realpath(parent)
    return path == parent or path.startswith(parent + os.sep)


def main():
    args = parse_args()
    if args.fake and not args.out:
        die("--fake needs --out DIR (a scratch directory outside the project)")
    if args.resume and args.fresh:
        die("--resume and --fresh exclude each other")
    out_root = args.out or (os.path.join(ROOT, "build", "quick-results") if args.quick else RESULTS_DIR)
    if args.quick and inside(out_root, RESULTS_DIR):
        die("--quick never writes into results/ (its rows are not measurements for the report)")
    if args.fake and inside(out_root, RESULTS_DIR):
        die("synthetic rows must never be stored under results/")

    env = dict(os.environ)
    if args.host:
        env["PC_HOST"] = args.host
    runner = Runner(args.bin_dir, args.fake, env)
    wanted = args.part or [p for p in PARTS if p not in OPTIONAL_PARTS or args.relax_yield or args.quick]
    parts = [p for p in wanted if runner.has(p)]
    for p in wanted:
        if not runner.has(p) and not (args.quick and p in OPTIONAL_PARTS):
            log("! %s: %s not found, part skipped (run `make` first)" % (p, os.path.join(args.bin_dir, PROGRAMS[p][0])))
    if not parts:
        die("no benchmark program found in %s (run `make` first)" % args.bin_dir)

    # ---- discovery: schema, implementation names, identity of the machine ----
    impls = {}
    for p in list(parts):
        header = runner.simple(p, "header").strip()
        if header != HEADER:
            die("%s prints a different CSV header than this script expects:\n  got      %s\n  expected %s"
                % (p, header, HEADER))
        impls[p] = [ln.strip() for ln in runner.simple(p, "list").splitlines() if ln.strip()]
        if not impls[p]:
            die("%s list printed no implementation" % p)
        if p == "lock_yield":                              # only useful where the hint really differs
            st, f, _, msg = runner.run(p, impls[p][0], 1, 100, 0, N_SHORT, CALIB_TIMEOUT_S)
            if st != "ok" or "relax:yield" not in f[16]:
                log("  lock_yield: %s does not use another spin-wait hint on this machine, part skipped"
                    % PROGRAMS[p][0])
                parts.remove(p)
                del impls[p]
    p0 = parts[0]
    status, f, _, msg = runner.run(p0, impls[p0][0], 1, 100, 0, N_SHORT, CALIB_TIMEOUT_S)
    if status != "ok":
        die("probe run of %s failed: %s" % (p0, msg))
    meta = f[:5]                                            # host, arch, os, compiler, ncpu
    host, ncpu = meta[0], int(meta[4])
    host_dir = re.sub(r"[^A-Za-z0-9._-]", "-", host) or "unknown"
    if host.upper().startswith("FAKE") and inside(out_root, RESULTS_DIR):
        die("the benchmark programs print synthetic rows (host=%s): refusing to write under results/" % host)
    out_dir = os.path.join(out_root, host_dir)
    mid = mid_core_count(ncpu) if not args.fake else (int(os.environ.get("FAKE_PCORES", "4")) or None)

    grid = QUICK_GRID if args.quick else GRID
    reps = args.reps or (QUICK_REPS if args.quick else REPS)
    impl_filter = [s for s in (args.impl or "").split(",") if s]
    configs = expand(grid, impls, ncpu, mid, impl_filter)
    if not configs:
        die("the grid is empty (check --part / --impl)")

    # ---- earlier files ----
    existing = [p for p in parts if os.path.exists(csv_path(out_dir, p))]
    if existing and args.quick and not args.resume:
        args.fresh = True                                   # quick results are disposable
    if existing and not (args.resume or args.fresh or args.dry_run):
        die("%s already holds %s.\n  --resume  continue that campaign (same iteration counts)\n"
            "  --fresh   archive it into old-<time>/ and start again"
            % (out_dir, ", ".join(p + ".csv" for p in existing)))
    calib_path = os.path.join(out_dir, "calibration.json")
    calib, done, timeouts = {}, set(), {}
    if args.resume and os.path.isdir(out_dir):
        if os.path.exists(calib_path):
            with open(calib_path) as fh:
                calib = json.load(fh)
        for p in parts:
            d, t = read_done(out_dir, p)
            done |= d
            timeouts.update(t)

    # ---- estimate and budget ----
    def total_estimate(scale):
        return sum(estimate(c, calib, scale) for c in configs for r in range(1, reps + 1)
                   if c.key + (r,) not in done)
    scale, nominal = 1.0, total_estimate(1.0)
    n_runs = sum(1 for c in configs for r in range(1, reps + 1) if c.key + (r,) not in done)
    limit = args.budget * 60.0 * (1.0 - BUDGET_RESERVE)
    if args.budget > 0 and not args.quick and nominal > limit:
        fixed = n_runs * OVERHEAD_S
        scale = max(MIN_SCALE, (limit - fixed) / max(1e-9, nominal - fixed))
    est = total_estimate(scale)

    log("host %s (%s, %s, %s), %d hardware threads%s" % (
        host, meta[1], meta[2], meta[3], ncpu, ", %d performance/physical cores" % mid if mid else ""))
    log("output: %s" % out_dir)
    for p in parts:
        cs = [c for c in configs if c.part == p]
        log("  %-10s %2d implementations: %s" % (p, len({c.impl for c in cs}), " ".join(sorted({c.impl for c in cs}))))
        for sw in sorted({c.sweep for c in cs}):
            sc = [c for c in cs if c.sweep == sw]
            line = "    sweep %-10s %4d configurations; threads %s" % (
                sw, len(sc), ",".join(str(t) for t in sorted({c.threads for c in sc})))
            if kind_of(p) != "barrier":
                line += "; outside_work %s; n %s" % (
                    ",".join(str(o) for o in sorted({c.ow for c in sc})),
                    ",".join(str(n) for n in sorted({c.n for c in sc})))
            log(line)
    log("%d configurations x %d repetitions = %d runs to do%s" % (
        len(configs), reps, n_runs, " (%d already stored)" % (len(configs) * reps - n_runs) if done else ""))
    if scale < 1.0:
        log("run lengths scaled by %.2f to fit the budget of %.0f min (--budget 0 keeps the nominal lengths: %s)"
            % (scale, args.budget, fmt_dur(nominal)))
        if total_estimate(scale) > limit * 1.02:
            log("! even at the smallest run length the estimate exceeds the budget; consider --reps or --part")
    tmo = [timeout_of(c, args.timeout) for c in configs]
    log("estimated duration: %s (model: lock runs %.1f x their lower bound, the others at their pilot time, "
        "plus %.0f ms per process; pilot runs not included)" % (fmt_dur(est), LOCK_SLACK, OVERHEAD_S * 1e3))
    log("  runs above the model make it longer; a configuration that times out (timeouts of %.0f-%.0f s) is "
        "repeated with %d times fewer iterations, and skipped after %d timeouts"
        % (min(tmo), max(tmo), REDUCE_FACTOR, SKIP_AFTER_TIMEOUTS))
    if args.dry_run:
        return 0

    # ---- load guard ----
    start_load = load1()
    deadline = time.monotonic() + args.wait * 60.0
    while start_load > args.load_max and not args.force and time.monotonic() < deadline:
        log("load average %.2f > %.1f, waiting ..." % (start_load, args.load_max))
        time.sleep(15)
        start_load = load1()
    if start_load > args.load_max and not args.force:
        die("1-minute load average is %.2f, above %.1f: the machine is not idle.\n"
            "  Close other programs and retry, use --wait MIN to wait, or --force to measure anyway."
            % (start_load, args.load_max))
    if start_load > args.load_max:
        log("! load average %.2f is above %.1f; continuing because of --force" % (start_load, args.load_max))

    # ---- output directory ----
    os.makedirs(out_dir, exist_ok=True)
    if args.fresh and existing:
        old = os.path.join(out_dir, "old-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        os.makedirs(old)
        for name in os.listdir(out_dir):
            if not name.startswith("old-"):
                shutil.move(os.path.join(out_dir, name), os.path.join(old, name))
        log("earlier files moved to %s" % old)
    logf = open(os.path.join(out_dir, "campaign.log"), "a")

    def logline(msg):
        logf.write("%s %s\n" % (datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), msg))
        logf.flush()
    logline("START argv=%s host=%s ncpu=%d reps=%d runs=%d seed=%d scale=%.3f load1=%.2f force=%d"
            % (" ".join(sys.argv[1:]) or "-", host, ncpu, reps, n_runs, args.seed, scale, start_load, args.force))

    info = os.path.join(ROOT, "scripts", "machine_info.sh")
    if os.path.exists(info):
        try:
            res = subprocess.run(["sh", info], capture_output=True, text=True, timeout=60,
                                 env=dict(env, PC_HOST=host), cwd=ROOT)
            with open(os.path.join(out_dir, "machine.txt"), "w") as fh:
                fh.write(res.stdout)
        except (OSError, subprocess.SubprocessError) as e:
            log("! machine_info.sh failed: %s" % e)
    if runner.has("sleep_latency"):
        try:
            with open(os.path.join(out_dir, "sleep_latency.csv"), "w") as fh:
                fh.write(runner.simple("sleep_latency", timeout=120.0))
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            log("! sleep_latency failed: %s" % e)
    else:
        log("! sleep_latency not found, sleep_latency.csv not written")

    # ---- the campaign ----
    counts = dict(ok=0, timeout=0, error=0, skipped=0, bad=0, reduced=0)
    t_start = time.monotonic()

    def finish(code, why):
        end_load = load1()
        dur = time.monotonic() - t_start
        logline("%s duration=%s ok=%d bad_check=%d timeout=%d reduced_configs=%d skipped=%d error=%d load1=%.2f" % (
            why, fmt_dur(dur), counts["ok"], counts["bad"], counts["timeout"], counts["reduced"],
            counts["skipped"], counts["error"], end_load))
        logf.close()
        log("%s after %s: %d rows ok, %d with ok=0 from the program, %d timeouts (%d configurations "
            "repeated with fewer iterations), %d skipped after timeouts, %d errors"
            % (why, fmt_dur(dur), counts["ok"], counts["bad"], counts["timeout"], counts["reduced"],
               counts["skipped"], counts["error"]))
        log("load average: %.2f at start, %.2f at end" % (start_load, end_load))
        log("results in %s" % out_dir)
        return code

    def on_signal(signum, _frame):
        runner.kill()
        log("\ninterrupted (signal %d); continue later with --resume" % signum)
        sys.exit(finish(130, "INTERRUPTED"))
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    calibrate(runner, configs, impls, calib, scale, calib_path, args.verbose)

    est_of = {c.key: estimate(c, calib, scale) for c in configs}      # refined by measurements
    left = {c.key: sum(1 for r in range(1, reps + 1) if c.key + (r,) not in done) for c in configs}
    remaining = sum(est_of[k] * left[k] for k in est_of)
    log("starting %d runs, estimated %s" % (n_runs, fmt_dur(remaining)))
    n_done, last_print = 0, 0.0
    for rep in range(1, reps + 1):
        jobs = [c for c in configs if c.key + (rep,) not in done]
        random.Random(args.seed + rep).shuffle(jobs)
        if jobs:
            log("-- repetition %d/%d: %d runs, load %.2f" % (rep, reps, len(jobs), load1()))
        for c in jobs:
            timeout = timeout_of(c, args.timeout)
            while True:                                     # a second pass only after a timeout
                iters = iters_of(c, calib)
                if timeouts.get(c.key, 0) >= SKIP_AFTER_TIMEOUTS:
                    status, wall, msg = "skipped", 0.0, "timeout_skipped"
                    fields = failed_row(meta, c, iters, timeout, "timeout_skipped")
                else:
                    status, fields, wall, msg = runner.run(c.part, c.impl, c.threads, iters, c.ow, c.n, timeout)
                    if status == "timeout":
                        timeouts[c.key] = timeouts.get(c.key, 0) + 1
                        fields = failed_row(meta, c, iters, timeout, "timeout")
                    elif status == "error":
                        fields = failed_row(meta, c, iters, 0.0, "error:" + msg)
                    elif fields[14].strip() != "1":
                        status, msg = "bad", "the program reports ok=0 (its result check failed)"
                append_row(out_dir, c.part, fields, rep, wall)
                counts[status] += 1
                retry = (status == "timeout" and timeouts[c.key] < SKIP_AFTER_TIMEOUTS and iters > MIN_ITERS)
                if retry:                                   # measure the collapse instead of only bounding it
                    fewer = max(MIN_ITERS, iters // REDUCE_FACTOR)
                    calib[c.rkey] = dict(iters=fewer, reduced_from=iters, reason="timeout after %.1f s" % timeout)
                    save_json(calib_path, calib)
                    counts["reduced"] += 1
                    msg += "; repeating with iters=%d" % fewer
                now = time.monotonic()
                if args.verbose or status not in ("ok", "skipped") or now - last_print >= 15.0:
                    last_print = now
                    res = "%.4f s" % float(fields[11]) if status in ("ok", "bad") else status.upper()
                    log("[rep %d/%d %5d/%d %5.1f%%] %s -> %s%s | elapsed %s, ETA %s" % (
                        rep, reps, n_done + 1, n_runs, 100.0 * (n_done + 1) / n_runs, c.label(), res,
                        " (" + msg + ")" if msg and status != "skipped" else "",
                        fmt_dur(now - t_start), fmt_dur(remaining)))
                if not retry:
                    break
            n_done += 1
            left[c.key] -= 1
            remaining -= est_of[c.key]
            if status in ("ok", "bad", "timeout"):          # later repetitions will cost about the same
                new = wall + (0.0 if status != "timeout" else 0.5)
                remaining += (new - est_of[c.key]) * left[c.key]
                est_of[c.key] = new
            elif status == "skipped":
                remaining -= est_of[c.key] * left[c.key]
                est_of[c.key] = 0.0
    return finish(0, "END")


if __name__ == "__main__":
    sys.exit(main())
