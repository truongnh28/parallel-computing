#!/usr/bin/env python3
"""Numbers from the extra apple-m5 measurements (results/apple-m5/extra/, experiments/run_extra.py).

handoff: per lock and thread count, medians over 5 runs of the hand-over statistics measured by
experiments/handoff_gap.cpp (a hand-over is "long" when it exceeds 100 us).
lowload: medians over 7 runs started at 1-minute load average < 3, next to the medians of the main
campaign (results/apple-m5/lock.csv) for the same lock, thread count, N = 1009, OUTSIDE_WORK = 0.
"""
import csv
import statistics as st
from collections import defaultdict
from pathlib import Path

A1 = Path(__file__).resolve().parents[2]
R = A1 / "results" / "apple-m5"


def handoff():
    g = defaultdict(list)
    for r in csv.DictReader(open(R / "extra" / "handoff.csv")):
        if r["ok"] == "1":
            g[(r["lock"], int(r["threads"]))].append(r)
    print("lock        p  runs  ns/CS   long%   mean_long_us  time_in_long%  max_gap_us  load1")
    for (lock, p), rs in sorted(g.items()):
        med = lambda c: st.median(float(r[c]) for r in rs)
        share = st.median(float(r["time_in_long_s"]) / float(r["seconds"]) for r in rs)
        print(f"{lock:10s} {p:2d}  {len(rs):4d}  {med('ns_per_cs'):7.0f}  {100*med('long_fraction'):6.3f}"
              f"  {med('mean_long_us'):12.0f}  {100*share:13.1f}  {med('max_gap_us'):10.0f}  {med('load1'):5.1f}")


def lowload():
    path = R / "extra" / "lowload.csv"
    if not path.exists():
        return
    low, main = defaultdict(list), defaultdict(list)
    for r in csv.DictReader(open(path)):
        if r["ok"] == "1":
            low[(r["impl"], int(r["threads"]))].append(r)
    for r in csv.DictReader(open(R / "lock.csv")):
        if r["n"] == "1009" and r["outside_work"] == "0" and r["ok"] == "1":
            main[(r["impl"], int(r["threads"]))].append(r)
    print("\nlock            p  runs  ns/CS(low)  ns/CS(main)  low/main  load1(low)  load1(main)")
    ns = lambda r: 1e9 * float(r["seconds"]) / float(r["ops"])
    for k in sorted(low):
        lo, ma = low[k], main.get(k, [])
        if not ma:
            continue
        a, b = st.median(map(ns, lo)), st.median(map(ns, ma))
        print(f"{k[0]:14s} {k[1]:2d}  {len(lo):4d}  {a:10.1f}  {b:11.1f}  {a/b:8.2f}"
              f"  {st.median(float(r['load1']) for r in lo):10.1f}  {st.median(float(r['load1']) for r in ma):11.1f}")


if __name__ == "__main__":
    handoff()
    lowload()
