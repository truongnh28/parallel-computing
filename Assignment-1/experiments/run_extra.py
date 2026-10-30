#!/usr/bin/env python3
"""Extra measurements for the report (apple-m5 only), run after the main campaign.

Part A (handoff): build/experiments/handoff_gap for mcs, ttas, tas, std_mutex at 4, 8, 10, 12
threads, N = 1009, OUTSIDE_WORK = 0, 5 repetitions in shuffled order. Tests the hypothesis that MCS
collapses at the core count because a few hand-overs to a thread without a core take a whole
scheduling slice.

Part B (low load): re-runs build/bench_lock for tas, ttas, mcs, std_mutex, os_unfair_lock at
N = 1009, OUTSIDE_WORK = 0, 1/2/4/8/10 threads, with exactly the iteration counts of the main
campaign, 7 repetitions in shuffled order, each run started only when the 1-minute load average is
below LOAD_MAX. Compares the medians with results/<host>/lock.csv to see whether the high load
average of the main campaign changed the results.

Output: results/<host>/extra/{handoff.csv, lowload.csv, run.log}. One process at a time; do not run
anything else heavy while this runs.
"""
import csv
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

A1 = Path(__file__).resolve().parents[1]
HOST = sys.argv[1] if len(sys.argv) > 1 else "apple-m5"
OUT = A1 / "results" / HOST / "extra"
LOAD_MAX = 3.0
SEED = 20261029


def load1():
    return os.getloadavg()[0]


def wait_low_load(log):
    t0 = time.time()
    while load1() > LOAD_MAX and time.time() - t0 < 120:
        time.sleep(5)
    if load1() > LOAD_MAX:
        log.write(f"# warning: load1 {load1():.2f} > {LOAD_MAX} after 120 s wait\n")


def part_a(log):
    exe = A1 / "build" / "experiments" / "handoff_gap"
    header = subprocess.run([str(exe), "header"], capture_output=True, text=True).stdout.strip()
    jobs = []
    for rep in range(5):
        batch = []
        for lock in ["mcs", "ttas", "tas", "std_mutex"]:
            for p in [4, 8, 10, 12]:
                total = 3000 if (lock == "mcs" and p >= 12) else 40000
                batch.append((rep, lock, p, max(1, total // p)))
        random.Random(SEED + rep).shuffle(batch)
        jobs += batch
    with open(OUT / "handoff.csv", "w") as f:
        f.write("rep," + header + "\n")
        for rep, lock, p, iters in jobs:
            wait_low_load(log)
            r = subprocess.run([str(exe), f"lock={lock}", f"threads={p}", f"iters={iters}"],
                               capture_output=True, text=True, timeout=120)
            f.write(f"{rep},{r.stdout.strip()}\n")
            f.flush()


def campaign_iters():
    """iters of each (lock, threads) at N=1009, OW=0 in the main campaign (most common value)."""
    rows = list(csv.DictReader(open(A1 / "results" / HOST / "lock.csv")))
    out = {}
    for r in rows:
        if r["n"] == "1009" and r["outside_work"] == "0" and r["ok"] == "1":
            out.setdefault((r["impl"], int(r["threads"])), []).append(int(r["iters"]))
    return {k: statistics.mode(v) for k, v in out.items()}


def part_b(log):
    exe = A1 / "build" / "bench_lock"
    iters = campaign_iters()
    configs = [(l, p) for l in ["tas", "ttas", "mcs", "std_mutex", "os_unfair_lock"] for p in [1, 2, 4, 8, 10]]
    with open(OUT / "lowload.csv", "w") as f:
        f.write(subprocess.run([str(exe), "header"], capture_output=True, text=True).stdout.strip()
                + ",rep\n")
        for rep in range(7):
            batch = list(configs)
            random.Random(SEED + 100 + rep).shuffle(batch)
            for lock, p in batch:
                wait_low_load(log)
                r = subprocess.run([str(exe), f"lock={lock}", f"threads={p}", f"iters={iters[(lock, p)]}",
                                    "outside_work=0", "n=1009"], capture_output=True, text=True, timeout=300)
                f.write(r.stdout.strip() + f",{rep}\n")
                f.flush()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "run.log", "w") as log:
        log.write(f"start {time.strftime('%Y-%m-%dT%H:%M:%S')} load1={load1():.2f}\n")
        part_a(log)
        log.write(f"part A done {time.strftime('%H:%M:%S')} load1={load1():.2f}\n")
        part_b(log)
        log.write(f"end {time.strftime('%Y-%m-%dT%H:%M:%S')} load1={load1():.2f}\n")


if __name__ == "__main__":
    main()
