#!/usr/bin/env python3
"""plot.py -- draws every figure and writes every table of the report from the raw rows.

    python3 scripts/plot.py                       results/*/  ->  report/figures/<host>/
    python3 scripts/plot.py --results DIR --figures DIR [--host NAME ...]

For every machine (one directory results/<host>/ written by run_bench.py):
    report/figures/<host>/lock_time.pdf           L1  execution time vs threads, one panel per OUTSIDE_WORK,
                                                      with the ideal lower bound (main N)
    report/figures/<host>/lock_throughput.pdf     L2  critical sections per second
    report/figures/<host>/lock_slowdown.pdf       L2  measured time / ideal lower bound
    report/figures/<host>/lock_cs_length.pdf      L3  effect of N (length of the critical section)
    report/figures/<host>/lock_backoff.pdf        L4  backoff variants relative to plain TAS / TTAS
    report/figures/<host>/lock_{time,slowdown,backoff}_short.pdf   the same for the short critical section
    report/figures/<host>/lock_fairness.pdf       L5  fastest thread / slowest thread (extra: thr_min_s, thr_max_s)
    report/figures/<host>/lock_relax.pdf              ISB vs YIELD spin-wait hint (only with lock_yield.csv)
    report/figures/<host>/barrier_rate.pdf        B1  barrier episodes per second, log y
    report/figures/<host>/counter_time.pdf        C1  execution time of the counters
    report/figures/<host>/counter_variants.pdf    C1b backoff / memory-order variants relative to the plain counter
    report/figures/<host>/counter_cas_failures.pdf C2 failed CAS attempts per increment (extra: cas_fail)
    report/figures/<host>/tables.tex              every table and every figure caption, as LaTeX macros:
                                                  machine, campaign, lockunit, locksingle, locktime, barrier,
                                                  countertime, casfail, sleeplatency, appendix (the other
                                                  OUTSIDE_WORK levels)
With more than one machine:
    report/figures/cross/{lock,barrier,counter}.pdf   X1  the same configuration on every machine
Always:
    report/figures/cross/tables.tex               machine-configuration table (one column per machine)

The iteration count differs between configurations (run_bench.py chooses it so that every run
has about the same length), so times are compared per iteration: every time drawn is the measured
time scaled to a common iteration count, stated in the caption.

Points are medians over the repetitions, error bars go from the minimum to the maximum. A run
that hit the timeout counts as "slower than everything else" in the median; a configuration
whose median is a timeout is drawn as a hollow triangle at the timeout (a lower bound of its
time) on a time axis and as an x at the bottom of a rate axis (a missing point, never a zero).
A configuration measured with fewer iterations (collapse, or after a timeout) gets a ring.
Rows with ok=0 that are not timeouts (failed result check, crashed run) are never plotted; they
are counted in the campaign table.

The report is in Vietnamese: axis labels, legends and captions are Vietnamese and numbers use a
decimal comma. Messages of this script are in English.

A missing CSV file or a missing implementation is skipped with a message, never an error.
Needs numpy and matplotlib.
"""
import argparse
import collections
import csv
import math
import os
import re
import sys

try:
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import ticker
    from matplotlib.lines import Line2D
except ImportError as exc:                                   # pragma: no cover
    sys.exit("plot.py needs numpy and matplotlib (%s).\n"
             "  python3 -m venv .venv && .venv/bin/pip install matplotlib numpy\n"
             "  make figures PYTHON=.venv/bin/python" % exc)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARTS = ("lock", "barrier", "counter", "lock_yield")      # one CSV file each; lock_yield is optional

# =================================================================================================
# Appearance. One implementation has the same colour, marker and line style in every figure.
#   colour + marker shape = algorithm family          (TAS, TTAS, MCS, a library lock, ...)
#   line style + marker fill = variant of the family  (no backoff, sleep, spin, yield, seq_cst)
# The six colours are the Okabe-Ito colour-blind-safe set; identity never depends on colour alone.
# =================================================================================================
BLUE, VERMILLION, GREEN = "#0072B2", "#D55E00", "#009E73"
PURPLE, ORANGE, SKY = "#CC79A7", "#E69F00", "#56B4E9"
DARK, GREY, BLACK = "#3b3b3b", "#8c8c8c", "#000000"

# (regular expression searched in the base name, colour, marker); the first match wins. The order
# of this list is also the order of the implementations in legends and tables.
FAMILY_STYLE = [
    (r"faa|fetch", BLACK, "d"),
    (r"cas", PURPLE, "h"),
    (r"^tas", BLUE, "o"),
    (r"^ttas", VERMILLION, "s"),
    (r"^mcs", GREEN, "^"),
    # sense-reversing barrier: "sr" spins, "sr_bo_*" are its backoff variants, "sr_block" blocks
    (r"^sr$", BLUE, "o"),
    (r"wait|block|futex", GREEN, "^"),
    (r"std_mutex|std_barrier|^std$", DARK, "D"),
    (r"pthread_mutex|mutex_cv|condvar|^cv|_cv", GREY, "v"),
    (r"os_unfair|pthread_spin|pthread_barrier", ORANGE, "P"),
    (r"omp", SKY, "*"),
    # barrier patterns for names this script has not seen (by the way they wait)
    (r"sleep", VERMILLION, "s"),
    (r"yield", PURPLE, "h"),
    (r"spinbo|spin_bo|backoff|bo$", SKY, "X"),
    (r"spin|busy", BLUE, "o"),
]
FALLBACK_STYLE = [(ORANGE, "p"), (SKY, "<"), (PURPLE, ">"), (GREEN, "8"), (BLUE, "H"), (VERMILLION, "X")]
# variant -> (line style, marker fill)
VARIANT_STYLE = {"": ("-", "full"), "sleep": ("--", "none"), "spin": (":", "left"),
                 "yield": ("-.", "bottom"), "seqcst": ((0, (6, 2)), "top")}
VARIANT_RE = re.compile(r"^(.+?)(?:_bo)?_(sleep|spin|yield|seqcst)$")
VARIANT_VN = {"sleep": "backoff ngủ (sleep_for)", "spin": "backoff quay (spin)",
              "yield": "quay rồi yield", "seqcst": "memory order seq_cst"}

FIG_WIDTH = 7.2            # inches; the report includes every figure at \linewidth
WATERMARK = "DỮ LIỆU GIẢ – KHÔNG PHẢI SỐ ĐO"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.titlesize": 10, "axes.labelsize": 10,
    "xtick.labelsize": 9, "ytick.labelsize": 9, "legend.fontsize": 9,
    "axes.spines.top": False, "axes.spines.right": False, "axes.linewidth": 0.6,
    "axes.grid": True, "grid.color": "#e3e3e3", "grid.linewidth": 0.5, "axes.axisbelow": True,
    "xtick.major.width": 0.6, "ytick.major.width": 0.6, "xtick.minor.width": 0.4, "ytick.minor.width": 0.4,
    "lines.linewidth": 1.3, "lines.markersize": 4.5, "errorbar.capsize": 1.8,
    "pdf.fonttype": 42, "savefig.bbox": "tight", "savefig.pad_inches": 0.03,
    "figure.constrained_layout.use": True,
})


def note(msg):
    print("plot: " + msg, flush=True)


def shown(path):
    rel = os.path.relpath(path, os.getcwd())
    return path if rel.startswith("..") else rel


# ---- numbers and LaTeX ------------------------------------------------------------------------------
def vn(x, sig=3, tex=False):
    """Number with `sig` significant digits, decimal comma, thin space between thousands."""
    if x is None or not math.isfinite(x):
        return "–"
    if x == 0:
        return "0"
    a = abs(x)
    digits = sig - 1 - int(math.floor(math.log10(a)))
    if digits <= 0:
        s = "{:,.0f}".format(round(x, digits))
        return s.replace(",", r"\," if tex else "\u2009")
    return ("%.*f" % (digits, x)).replace(".", ",")


def vint(x, tex=False):
    """Integer with a thin space between thousands."""
    if x is None:
        return "–"
    return "{:,d}".format(int(round(x))).replace(",", r"\," if tex else "\u2009")


def vn_plain(x):
    """Tick label: shortest representation, decimal comma."""
    return ("%g" % x).replace(".", ",")


def tex(s):
    out = []
    for ch in str(s):
        if ch in "&%$#_{}":
            out.append("\\" + ch)
        elif ch == "\\":
            out.append(r"\textbackslash{}")
        elif ch == "~":
            out.append(r"\textasciitilde{}")
        elif ch == "^":
            out.append(r"\textasciicircum{}")
        else:
            out.append(ch)
    return "".join(out)


def tt(s):
    return r"\texttt{%s}" % tex(s)


def si_time(seconds, tex_mode=True):
    """Human-readable duration with a decimal comma: 12,3 ns / 1,02 µs / 0,123 s."""
    if seconds is None or not math.isfinite(seconds):
        return "–"
    for unit, f in (("s", 1.0), ("ms", 1e-3), ("µs", 1e-6), ("ns", 1e-9)):
        if seconds >= f or unit == "ns":
            return vn(seconds / f, 3, tex_mode) + (r"\," if tex_mode else " ") + unit
    return "–"


def big(x, tex_mode=True):
    """Rate with a suffix: 1,23 M / 45,6 k / 789."""
    if x is None or not math.isfinite(x):
        return "–"
    for suffix, f in (("M", 1e6), ("k", 1e3)):
        if x >= f:
            return vn(x / f, 3, tex_mode) + (r"\," if tex_mode else " ") + suffix
    return vn(x, 3, tex_mode)


# ---- data -------------------------------------------------------------------------------------------
def parse_extra(text):
    d = {}
    for item in (text or "").split(";"):
        if ":" in item:
            k, v = item.split(":", 1)
            d[k.strip()] = v.strip()
    return d


def load_csv(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            try:
                row = dict(impl=r["impl"], threads=int(r["threads"]), iters=int(r["iters"]),
                           ow=int(r["outside_work"]), n=int(r["n"]), seconds=float(r["seconds"]),
                           ops=float(r["ops"]), rate=float(r["ops_per_s"]), ok=int(r["ok"]),
                           load1=float(r["load1"]), extra=parse_extra(r.get("extra")),
                           extra_raw=r.get("extra") or "", rep=int(r.get("rep") or 0),
                           timestamp=r.get("timestamp") or "", ncpu=int(r["ncpu"]),
                           arch=r["arch"], os=r["os"], compiler=r["compiler"], host=r["host"])
            except (KeyError, ValueError, TypeError):
                continue                                     # truncated or foreign line
            rows.append(row)
    return rows


class Stat:
    """All repetitions of one configuration (impl, threads, outside_work, n)."""

    def __init__(self, impl, threads, ow, n):
        self.impl, self.threads, self.ow, self.n = impl, threads, ow, n
        self.ok_rows, self.cens_rows, self.bad = [], [], 0

    def close(self):
        # The iteration count of a configuration is fixed for a campaign, with one exception:
        # after a timeout run_bench.py repeats the configuration with fewer iterations. The valid
        # rows then all have the smaller count, and the timeout at the larger count is not a
        # repetition of the same experiment any more (self.reduced marks such a configuration).
        self.x = collections.defaultdict(list)
        self.sec, self.rate, self.cens, self.ops, self.reduced, self.mixed = [], [], [], 0.0, False, False
        if self.ok_rows:
            counts = collections.Counter(r["iters"] for r in self.ok_rows)
            self.iters = counts.most_common(1)[0][0]
            self.mixed = len(counts) > 1
            self.cens = [r["seconds"] for r in self.cens_rows if r["iters"] == self.iters]
            self.reduced = any(r["iters"] > self.iters for r in self.cens_rows)
            for r in self.ok_rows:
                if r["iters"] != self.iters:
                    continue
                self.sec.append(r["seconds"])
                self.rate.append(r["rate"])
                self.ops = r["ops"]
                e = r["extra"]
                try:
                    if "thr_min_s" in e and float(e["thr_max_s"]) > 0:
                        self.x["fair"].append(float(e["thr_min_s"]) / float(e["thr_max_s"]))
                    if "cas_fail" in e and r["ops"] > 0:
                        self.x["cas_fail"].append(float(e["cas_fail"]) / r["ops"])
                except (KeyError, ValueError):
                    pass
                if r["ops"] > 0:
                    self.x["ns_per_op"].append(r["seconds"] / r["ops"] * 1e9)
        else:
            its = collections.Counter(r["iters"] for r in self.cens_rows)
            self.iters = min(its) if its else 0             # the last (smallest) size that was tried
            self.cens = [r["seconds"] for r in self.cens_rows if r["iters"] == self.iters]
            self.reduced = len(its) > 1
        vals = sorted(self.sec + [math.inf] * len(self.cens))
        self.med = float(np.median(vals)) if vals else math.nan
        self.cap = max(self.cens) if self.cens else math.nan
        self.censored = bool(vals) and not math.isfinite(self.med)
        self.lo = min(self.sec) if self.sec else self.cap
        self.hi = self.cap if self.cens else (max(self.sec) if self.sec else math.nan)
        self.reps = len(vals)
        self.valid = bool(vals)

    def triple(self, scale=1.0):
        """(median, low, high, censored, reduced) of the time, multiplied by scale."""
        if self.censored:
            return self.cap * scale, self.cap * scale, self.cap * scale, True, self.reduced
        return self.med * scale, self.lo * scale, self.hi * scale, False, self.reduced

    def xtriple(self, key):
        v = self.x.get(key)
        if not v or self.censored:
            return None
        return float(np.median(v)), min(v), max(v), False, self.reduced


def aggregate(rows, peers=False):
    """peers=True (locks): all implementations share the iteration count of a (threads, OW, N)
    cell, so a configuration with far fewer iterations was reduced on purpose (run_bench.py,
    REDUCED_ITERS) and is marked like one that was reduced after a timeout."""
    st = {}
    for r in rows:
        key = (r["impl"], r["threads"], r["ow"], r["n"])
        s = st.get(key)
        if s is None:
            s = st[key] = Stat(*key)
        if r["extra_raw"].startswith("timeout"):
            s.cens_rows.append(r)
        elif r["ok"] != 1 or r["seconds"] <= 0:
            s.bad += 1
        else:
            s.ok_rows.append(r)
    for s in st.values():
        s.close()
        if s.mixed:
            note("  ! %s p=%d ow=%d n=%d has valid rows with different iteration counts; "
                 "only those with iters=%d are used" % (s.impl, s.threads, s.ow, s.n, s.iters))
    if peers:
        cell = collections.defaultdict(list)
        for s in st.values():
            if s.valid and s.iters:
                cell[(s.threads, s.ow, s.n)].append(s.iters)
        for s in st.values():
            if s.valid and s.iters and s.iters * 10 < float(np.median(cell[(s.threads, s.ow, s.n)])):
                s.reduced = True
    return {k: s for k, s in st.items() if s.valid or s.bad}


class Host:
    def __init__(self, path):
        self.dir = path
        self.name = os.path.basename(os.path.normpath(path))
        self.rows = {p: load_csv(os.path.join(path, p + ".csv")) for p in PARTS}
        self.stats = {p: aggregate(r, peers=p.startswith("lock")) for p, r in self.rows.items()}
        self.machine = {}
        mpath = os.path.join(path, "machine.txt")
        if os.path.exists(mpath):
            with open(mpath, errors="replace") as f:
                for line in f:
                    if line.startswith("---- raw"):
                        break
                    if ": " in line:
                        k, v = line.split(": ", 1)
                        self.machine[k.strip()] = v.strip()
        any_rows = [r for rows in self.rows.values() for r in rows]
        self.empty = not any_rows
        first = any_rows[0] if any_rows else {}
        self.ncpu = first.get("ncpu") or self._int("logical_cpus") or 0
        self.arch, self.os, self.compiler = first.get("arch", ""), first.get("os", ""), first.get("compiler", "")
        self.synthetic = self.name.upper().startswith("FAKE") or str(first.get("host", "")).upper().startswith("FAKE")
        # the second marker on the thread axis: performance cores, or physical cores under SMT
        self.mid, self.mid_label = None, ""
        p, phys = self._int("performance_cores"), self._int("physical_cores")
        if p and 0 < p < self.ncpu:
            self.mid, self.mid_label = p, "số P-core (%d)" % p
        elif phys and 0 < phys < self.ncpu:
            virt = " máy ảo báo" if self.machine.get("virtualization") else ""
            self.mid, self.mid_label = phys, "số core vật lý%s (%d)" % (virt, phys)
        relax = {r["extra"].get("relax") for r in self.rows["lock"] if r["extra"].get("relax")}
        self.any_reduced = any(s.reduced for st in self.stats.values() for s in st.values())
        self.relax = ", ".join(sorted(relax))

    def _int(self, key):
        try:
            return int(str(self.machine.get(key, "")).split()[0])
        except (ValueError, IndexError):
            return None

    def impls(self, part):
        seen = []
        for r in self.rows[part]:
            if r["impl"] not in seen:
                seen.append(r["impl"])
        return seen

    def reps(self, part):
        return max([s.reps for s in self.stats[part].values()] or [0])


# ---- styles --------------------------------------------------------------------------------------------
class Styles:
    """Fixed appearance of every implementation name seen in any results directory."""

    def __init__(self, names_by_part):
        self.names = names_by_part
        self.fallback = {}
        self.cache = {}

    def split(self, part, impl):
        m = VARIANT_RE.match(impl)
        if m and m.group(1) in self.names[part]:
            return m.group(1), m.group(2)
        return impl, ""

    def get(self, part, impl):
        if (part, impl) in self.cache:
            return self.cache[(part, impl)]
        base, variant = self.split(part, impl)
        fam = base[5:] if part == "counter" and base.startswith("lock_") else base
        fam_base, fam_variant = self.split("lock", fam) if part == "counter" else (fam, "")
        if fam_variant:                                    # counter on a lock with backoff
            fam, variant = fam_base, fam_variant
        colour = marker = None
        for pattern, c, m in FAMILY_STYLE:
            if re.search(pattern, fam):
                colour, marker = c, m
                break
        if colour is None:
            if fam not in self.fallback:
                self.fallback[fam] = FALLBACK_STYLE[len(self.fallback) % len(FALLBACK_STYLE)]
                note("  no style rule for '%s' (%s): using a fallback style; add it to FAMILY_STYLE" % (fam, part))
            colour, marker = self.fallback[fam]
        ls, fill = VARIANT_STYLE.get(variant, ("-", "full"))
        st = dict(color=colour, marker=marker, linestyle=ls, fillstyle=fill,
                  markeredgecolor=colour, markerfacecolor=colour if fill == "full" else "white",
                  markerfacecoloralt="white", markeredgewidth=0.8)
        if fill not in ("full", "none"):
            st["markerfacecolor"] = colour
        self.cache[(part, impl)] = st
        return st

    def plain(self, part, impls):
        return [i for i in impls if self.split(part, i)[1] == "" and
                not (part == "counter" and i.startswith("lock_") and self.split("lock", i[5:])[1])]

    def order(self, part, impls):
        """Stable display order: families in the order of FAMILY_STYLE, plain before variants."""
        def key(i):
            base, variant = self.split(part, i)
            fam = base[5:] if part == "counter" and base.startswith("lock_") else base
            fb, fv = self.split("lock", fam) if part == "counter" else (fam, "")
            if fv:
                fam, variant = fb, fv
            rank = next((k for k, (pat, _, _) in enumerate(FAMILY_STYLE) if re.search(pat, fam)), 99)
            group = 0 if part != "counter" else (1 if i.startswith("lock_") else 0)
            return (group, rank, list(VARIANT_STYLE).index(variant) if variant in VARIANT_STYLE else 9, i)
        return sorted(impls, key=key)


# ---- drawing helpers -------------------------------------------------------------------------------------
def draw(ax, pts, style, label=None, connect=True, zero_ok=False):
    """pts: list of (x, median, low, high, censored, reduced). Returns (any censored, any reduced)."""
    fin = [q for q in pts if not q[4] and q[1] is not None and math.isfinite(q[1]) and (q[1] > 0 or zero_ok)]
    cen = [q for q in pts if q[4] and math.isfinite(q[1])]
    st = dict(style)
    if not connect:
        st["linestyle"] = "none"
    if fin:
        x = np.array([q[0] for q in fin], float)
        m = np.array([q[1] for q in fin], float)
        lo = np.array([q[2] for q in fin], float)
        hi = np.array([q[3] for q in fin], float)
        ax.errorbar(x, m, yerr=[np.maximum(m - lo, 0), np.maximum(hi - m, 0)], elinewidth=0.7,
                    label=label, **st)
    if cen:
        ax.plot([q[0] for q in cen], [q[1] for q in cen], linestyle="none", marker="^", markersize=6.5,
                markerfacecolor="white", markeredgecolor=style["color"], markeredgewidth=1.1,
                label=None if fin else label, clip_on=False, zorder=3)
    red = [q for q in fin if len(q) > 5 and q[5]]
    if red:                                                  # measured with fewer iterations after a timeout
        ax.plot([q[0] for q in red], [q[1] for q in red], linestyle="none", marker="o", markersize=9.5,
                markerfacecolor="none", markeredgecolor="0.1", markeredgewidth=0.7, zorder=4)
    return bool(cen), bool(red)


def mark_missing(ax, xs, style):
    """A configuration whose median is a timeout has no value on a rate axis: an x at the bottom."""
    if xs:
        ax.plot(xs, [0.035] * len(xs), transform=ax.get_xaxis_transform(), linestyle="none", marker="x",
                markersize=6, markeredgewidth=1.4, color=style["color"], clip_on=False, zorder=5)
    return bool(xs)


MISSING_HANDLE = dict(linestyle="none", marker="x", markersize=6, markeredgewidth=1.4, color="0.25",
                      label="vượt thời hạn (không có giá trị)")
MISSING_NOTE = (" Dấu × ở đáy trục: trung vị của cấu hình vượt thời hạn của một lần chạy nên không có giá trị; "
                "đó là điểm thiếu, không phải giá trị 0.")


class Flags:
    """Collects, over all series of a figure, whether timeouts / reduced runs were drawn."""

    def __init__(self):
        self.cens = self.red = False

    def add(self, result):
        self.cens |= result[0]
        self.red |= result[1]

    def handles(self):
        h = []
        if self.cens:
            h.append(Line2D([], [], linestyle="none", marker="^", markersize=6.5, markerfacecolor="white",
                            markeredgecolor="0.25", markeredgewidth=1.1, label="vượt thời hạn (thời gian thật lớn hơn)"))
        if self.red:
            h.append(Line2D([], [], linestyle="none", marker="o", markersize=9.5, markerfacecolor="none",
                            markeredgecolor="0.1", markeredgewidth=0.7, label="đo với số vòng lặp giảm 100 lần hoặc hơn"))
        return h

    def caption(self):
        return ""  # the markers are explained once in the report (method section)
        t = ""
        if self.cens:
            t += (" Tam giác rỗng: trung vị của cấu hình vượt thời hạn của một lần chạy, thời gian thật lớn hơn "
                  "giá trị vẽ.")
        if self.red:
            t += (" Điểm có vòng tròn bao quanh: cấu hình sụp đổ (hoặc đã vượt thời hạn ở số vòng lặp đầy đủ) nên "
                  "được đo với số vòng lặp nhỏ hơn 100 lần; giá trị vẽ đã quy đổi tuyến tính về cùng số vòng lặp với "
                  "các điểm khác.")
        return t


def thread_axis(ax, threads, host):
    ax.set_xscale("log", base=2)
    ax.set_xticks(threads)
    ax.set_xticklabels([str(t) for t in threads])
    ax.xaxis.set_minor_locator(ticker.NullLocator())
    ax.set_xlim(min(threads) / 1.15, max(threads) * 1.15)
    if host.ncpu:
        ax.axvline(host.ncpu, color="0.35", lw=0.9, zorder=1.2)
    if host.mid:
        ax.axvline(host.mid, color="0.35", lw=0.9, ls=(0, (1, 2)), zorder=1.2)
    labels = ax.get_xticklabels()
    keep = (host.ncpu, host.mid, threads[0], threads[-1])
    shown = threads[0]
    for i, (t, lab) in enumerate(zip(threads, labels)):  # drop labels that would touch a neighbour
        if i == 0 or len(threads) <= 8:
            continue
        nxt = threads[i + 1] if i + 1 < len(threads) else None
        close_prev = math.log2(t / shown) < 0.45
        close_next = nxt is not None and nxt in keep and math.log2(nxt / t) < 0.45
        if t not in keep and (close_prev or close_next):
            lab.set_visible(False)
        else:
            shown = t


def ncpu_label(host):
    """Name of the solid marker: with SMT the OS counts hardware threads, not cores."""
    phys = host._int("physical_cores")
    if phys and phys < host.ncpu:
        return "số luồng phần cứng (%d)" % host.ncpu
    return "số core của máy (%d)" % host.ncpu


def core_handles(host):
    h = []
    if host.ncpu:
        h.append(Line2D([], [], color="0.35", lw=0.9, label=ncpu_label(host)))
    if host.mid:
        h.append(Line2D([], [], color="0.35", lw=0.9, ls=(0, (1, 2)), label=host.mid_label))
    return h


def log_y(ax):
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(_log_label))
    ax.yaxis.set_minor_formatter(ticker.NullFormatter())


def _log_label(v, _pos=None):
    if v <= 0:
        return ""
    e = math.log10(v)
    if abs(e - round(e)) > 1e-6:
        return ""
    if 1e-3 <= v < 1e5:
        return vn_plain(round(v, 6)) if v < 1e4 else vn(v, 1)
    return r"$10^{%d}$" % int(round(e))


def tighten_log_y(axes):
    """A log axis that spans less than about a decade and a half gets labels at 2 and 5 as well."""
    for ax in axes:
        if ax.get_yscale() != "log" or not ax.get_visible():
            continue
        lo, hi = ax.get_ylim()
        if lo > 0 and math.log10(hi / lo) < 1.6:
            ax.yaxis.set_minor_locator(ticker.LogLocator(base=10, subs=(2, 5)))
            ax.yaxis.set_minor_formatter(ticker.FuncFormatter(lambda v, p: vn_plain(float("%.2g" % v))))


def comma_axis(ax, which="y"):
    axis = ax.yaxis if which == "y" else ax.xaxis
    axis.set_major_formatter(ticker.FuncFormatter(lambda v, p: vn_plain(round(v, 10))))


def panels(count, ncols=3, height=2.25, sharey=True, width=FIG_WIDTH):
    ncols = max(1, min(ncols, count))
    nrows = int(math.ceil(count / ncols))
    fig, axs = plt.subplots(nrows, ncols, figsize=(width, height * nrows + 0.2), sharey=sharey, squeeze=False)
    flat = list(axs.flat)
    for ax in flat[count:]:
        ax.set_visible(False)
    return fig, flat[:count], flat[count:]


def legend(fig, spare, handles, ncol=4, title=None):
    """Put the legend into an unused cell of the grid when there is one, else above the panels."""
    seen, uniq = set(), []
    for h in handles:
        if h.get_label() not in seen and not h.get_label().startswith("_"):
            seen.add(h.get_label())
            uniq.append(h)
    if spare:
        ax = spare[0]
        ax.set_visible(True)
        ax.axis("off")
        ax.legend(handles=uniq, loc="center", frameon=False, title=title, ncol=1 if len(uniq) <= 10 else 2,
                  handlelength=3.2)
    else:
        fig.legend(handles=uniq, loc="outside upper center", frameon=False, title=title,
                   ncol=min(ncol, 4, len(uniq)), handlelength=2.4, columnspacing=1.0)



def supylabel(fig, text, **kw):
    """Y label for the figure. With a figure-level legend above a single row of panels, the label goes on the
    first panel so that it is centred on the axes and does not run into the legend."""
    first = [a for a in fig.axes if a.get_visible() and a.get_subplotspec() is not None
             and a.get_subplotspec().is_first_col()]
    if fig.legends and len(first) == 1:
        first[0].set_ylabel(text, **kw)
    else:
        fig.supylabel(text, **kw)

def proxy(style, label):
    return Line2D([], [], label=label, **style)


def save(fig, host_or_none, out_dir, name, synthetic):
    if synthetic:
        fig.text(0.5, 0.5, WATERMARK, rotation=24, ha="center", va="center", fontsize=26,
                 color="#b00020", alpha=0.16, zorder=100)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".pdf")
    fig.savefig(path)
    plt.close(fig)
    note("  wrote %s" % shown(path))


# ---- the lock model ----------------------------------------------------------------------------------------
def unit_time(st, n, ow):
    """t = time of one is_prime(n) call, from the 1-thread runs with this OUTSIDE_WORK: the
    fastest lock's median divided by the number of calls. (It contains that lock's uncontended
    acquire/release cost divided by 1 + OW, so the bound drawn from it is slightly generous.)"""
    c = [s.med / (s.iters * (1 + ow)) for s in st.values()
         if s.threads == 1 and s.n == n and s.ow == ow and not s.censored and s.sec and s.iters]
    return min(c) if c else None


def unit_time_any(st, n, ow):
    t = unit_time(st, n, ow)
    if t is None:
        c = [unit_time(st, n, o) for o in sorted({s.ow for s in st.values() if s.n == n})]
        c = [x for x in c if x]
        t = min(c) if c else None
    return t


def bound(iters, t, p, ow):
    return iters * t * max(p, 1 + ow)


def ref_iters(st, n, ow):
    """Iteration count that the times of one (N, OUTSIDE_WORK) panel are scaled to: that of the
    1-thread runs (so the 1-thread point is the measured time), else the largest one."""
    one = [s.iters for s in st.values() if s.n == n and s.ow == ow and s.threads == 1 and s.iters and not s.reduced]
    if one:
        return collections.Counter(one).most_common(1)[0][0]
    return max([s.iters for s in st.values() if s.n == n and s.ow == ow and s.iters] or [0])


def main_n(st):
    """The N with the widest sweep (most thread counts x contention levels)."""
    cover = collections.Counter()
    for combo in {(s.n, s.ow, s.threads) for s in st.values()}:
        cover[combo[0]] += 1
    return max(cover, key=lambda n: (cover[n], -abs(n - 1000))) if cover else None


def synthetic_prefix(host):
    return r"\textbf{[DỮ LIỆU GIẢ để thử công cụ]} " if host.synthetic else ""


def stat_note(reps):
    """Medians, error bars, markers and circles are explained once in the report (method section)."""
    return ""


def cores_note(host):
    return ""


# ---- figures: locks ------------------------------------------------------------------------------------------
def lock_roles(st):
    """{"": main N, "_short": a smaller N that also has a thread sweep} -- the suffix of the file names."""
    n_main = main_n(st)
    roles = collections.OrderedDict()
    if n_main is None:
        return roles
    roles[""] = n_main
    sweeps = collections.defaultdict(set)
    for s in st.values():
        sweeps[s.n].add(s.threads)
    short = [n for n in sorted(sweeps) if n < n_main and len(sweeps[n]) >= 5]
    if short:
        roles["_short"] = short[0]
    return roles


def fig_lock_time(host, sty, out, cap):
    st = host.stats["lock"]
    impls = sty.order("lock", sty.plain("lock", host.impls("lock")))
    roles = lock_roles(st)
    if not roles or not impls:
        return note("  lock_time: no data, skipped")
    for suffix, n in roles.items():
        ows = sorted({s.ow for s in st.values() if s.n == n})
        threads = sorted({s.threads for s in st.values() if s.n == n})
        fig, axs, spare = panels(len(ows))
        fl = Flags()
        for ax, ow in zip(axs, ows):
            t, iref = unit_time_any(st, n, ow), ref_iters(st, n, ow)
            for impl in impls:
                pts = [(p,) + st[(impl, p, ow, n)].triple(iref / st[(impl, p, ow, n)].iters) for p in threads
                       if (impl, p, ow, n) in st and st[(impl, p, ow, n)].valid and st[(impl, p, ow, n)].iters]
                fl.add(draw(ax, pts, sty.get("lock", impl)))
            if t:
                xs = sorted(set(threads) | ({1 + ow} if threads[0] <= 1 + ow <= threads[-1] else set()))
                ax.plot(xs, [bound(iref, t, p, ow) for p in xs], color=BLACK, lw=1.0, ls=(0, (4, 2)), zorder=1.5)
            thread_axis(ax, threads, host)
            log_y(ax)
            ax.set_title("OUTSIDE_WORK = %d\n(%s %% việc nằm trong critical section)" % (ow, vn(100.0 / (1 + ow), 3)),
                         fontsize=9.5)
        handles = [proxy(sty.get("lock", i), i) for i in impls]
        handles.append(Line2D([], [], color=BLACK, lw=1.0, ls=(0, (4, 2)), label="cận dưới lý tưởng"))
        legend(fig, spare, handles + core_handles(host) + fl.handles())
        fig.supxlabel("Số thread", fontsize=10)
        supylabel(fig, "Thời gian chạy (s)", fontsize=10)
        tighten_log_y(axs)
        save(fig, host, out, "lock_time" + suffix, host.synthetic)
        cap["lock_time" + suffix] = (
            synthetic_prefix(host) +
            r"Thời gian chạy của chương trình khung theo số thread, mỗi ô một giá trị \texttt{OUTSIDE\_WORK}, "
            r"$N = %d$ (một lời gọi \texttt{is\_prime} tốn khoảng %s). Số vòng lặp của mỗi thread được chọn riêng "
            r"cho từng cấu hình để mọi lần chạy dài cỡ như nhau; hình vẽ thời gian đo nhân với $I/I_{\text{đo}}$, tức "
            r"thời gian chạy nếu mỗi thread làm cùng $I$ vòng lặp như lần chạy 1 thread của ô đó "
            r"(bảng~\ref{tab:%s:lockunit}). %s Đường đứt nét: cận dưới lý tưởng "
            r"$\max\bigl(p\,I\,t,\ (1+\mathrm{OW})\,I\,t\bigr)$, với $t$ suy ra từ lần chạy 1 thread nhanh nhất.%s%s"
            % (n, si_time(unit_time_any(st, n, ows[0])), host.name, stat_note(host.reps("lock")), cores_note(host),
               fl.caption()))


def _t1(st, n, ow):
    c = [s.med for s in st.values() if s.threads == 1 and s.n == n and s.ow == ow and s.sec]
    return min(c) if c else None


def fig_lock_throughput(host, sty, out, cap):
    st = host.stats["lock"]
    n = main_n(st)
    impls = sty.order("lock", sty.plain("lock", host.impls("lock")))
    if n is None or not impls:
        return note("  lock_throughput: no data, skipped")
    ows = sorted({s.ow for s in st.values() if s.n == n})
    threads = sorted({s.threads for s in st.values() if s.n == n})
    top = max([max(s.rate) for s in st.values() if s.n == n and s.rate] or [1.0])
    unit, uname = (1e6, "triệu") if top >= 2e6 else (1e3, "nghìn") if top >= 2e3 else (1.0, "")
    fig, axs, spare = panels(len(ows))
    fl, skipped = Flags(), False
    for ax, ow in zip(axs, ows):
        t = unit_time_any(st, n, ow)
        for impl in impls:
            pts, miss = [], []
            for p in threads:
                s = st.get((impl, p, ow, n))
                if s and s.rate and not s.censored:
                    pts.append((p, float(np.median(s.rate)) / unit, min(s.rate) / unit, max(s.rate) / unit,
                                False, s.reduced))
                elif s and s.censored:
                    miss.append(p)
            fl.add(draw(ax, pts, sty.get("lock", impl)))
            skipped |= mark_missing(ax, miss, sty.get("lock", impl))
        if t:
            xs = sorted(set(threads) | ({1 + ow} if threads[0] <= 1 + ow <= threads[-1] else set()))
            ax.plot(xs, [p / (max(p, 1 + ow) * t) / unit for p in xs], color=BLACK, lw=1.0, ls=(0, (4, 2)), zorder=1.5)
        thread_axis(ax, threads, host)
        ax.set_ylim(bottom=0)
        comma_axis(ax)
        ax.set_title("OUTSIDE_WORK = %d" % ow, fontsize=9.5)
    handles = [proxy(sty.get("lock", i), i) for i in impls]
    handles.append(Line2D([], [], color=BLACK, lw=1.0, ls=(0, (4, 2)), label="cận trên lý tưởng"))
    legend(fig, spare, handles + core_handles(host) + fl.handles()
           + ([Line2D([], [], **MISSING_HANDLE)] if skipped else []))
    fig.supxlabel("Số thread", fontsize=10)
    supylabel(fig, "Số critical section mỗi giây" + (" (%s)" % uname if uname else ""), fontsize=10)
    save(fig, host, out, "lock_throughput", host.synthetic)
    cap["lock_throughput"] = (
        synthetic_prefix(host) +
        r"Cùng số liệu với hình thời gian chạy, vẽ theo thông lượng: số critical section hoàn thành mỗi giây "
        r"(tổng của mọi thread), $N = %d$. Đường đứt nét: cận trên lý tưởng $p / \bigl(\max(p,\ 1+\mathrm{OW})\,t\bigr)$; "
        r"lock bão hoà khi $p > 1 + \mathrm{OW}$. %s%s%s%s"
        % (n, stat_note(host.reps("lock")), cores_note(host), fl.caption(), MISSING_NOTE if skipped else ""))


def fig_lock_slowdown(host, sty, out, cap):
    st = host.stats["lock"]
    impls = sty.order("lock", sty.plain("lock", host.impls("lock")))
    roles = lock_roles(st)
    if not roles or not impls:
        return note("  lock_slowdown: no data, skipped")
    for suffix, n in roles.items():
        ows = sorted({s.ow for s in st.values() if s.n == n})
        threads = sorted({s.threads for s in st.values() if s.n == n})
        fig, axs, spare = panels(len(ows))
        fl = Flags()
        for ax, ow in zip(axs, ows):
            t = unit_time_any(st, n, ow)
            if not t:
                continue
            for impl in impls:
                pts = []
                for p in threads:
                    s = st.get((impl, p, ow, n))
                    if s and s.valid and s.iters:
                        pts.append((p,) + s.triple(1.0 / bound(s.iters, t, p, ow)))
                fl.add(draw(ax, pts, sty.get("lock", impl)))
            ax.axhline(1.0, color=BLACK, lw=1.0, ls=(0, (4, 2)), zorder=1.5)
            thread_axis(ax, threads, host)
            log_y(ax)
            ax.set_title("OUTSIDE_WORK = %d" % ow, fontsize=9.5)
        handles = [proxy(sty.get("lock", i), i) for i in impls]
        handles.append(Line2D([], [], color=BLACK, lw=1.0, ls=(0, (4, 2)), label="cận dưới lý tưởng (= 1)"))
        legend(fig, spare, handles + core_handles(host) + fl.handles())
        fig.supxlabel("Số thread", fontsize=10)
        supylabel(fig, "Thời gian đo / cận dưới lý tưởng", fontsize=10)
        tighten_log_y(axs)
        save(fig, host, out, "lock_slowdown" + suffix, host.synthetic)
        cap["lock_slowdown" + suffix] = (
            synthetic_prefix(host) +
            r"Thời gian đo chia cho cận dưới lý tưởng $\max\bigl(p\,I\,t,\ (1+\mathrm{OW})\,I\,t\bigr)$, $N = %d$, mỗi ô "
            r"một mức \texttt{OUTSIDE\_WORK}. Giá trị 1 nghĩa là lock không thêm chi phí nào ngoài phần việc tuần tự "
            r"bắt buộc." % n
)


def fig_lock_cs_length(host, sty, out, cap):
    st = host.stats["lock"]
    impls = sty.order("lock", sty.plain("lock", host.impls("lock")))
    by_ow = collections.defaultdict(set)
    for s in st.values():
        by_ow[s.ow].add(s.n)
    if not by_ow or max(len(v) for v in by_ow.values()) < 2 or not impls:
        return note("  lock_cs_length: fewer than two values of N at the same OUTSIDE_WORK, skipped")
    ow = max(by_ow, key=lambda o: (len(by_ow[o]), -o))
    ns = sorted(by_ow[ow])
    per_thread = collections.defaultdict(set)
    for s in st.values():
        if s.ow == ow:
            per_thread[s.threads].add(s.n)
    threads = sorted(p for p, v in per_thread.items() if len(v) == len(ns))
    if len(threads) > 4:                                      # keep 1, the core counts and the largest
        keep = {threads[0], threads[-1], host.ncpu, host.mid}
        threads = [p for p in threads if p in keep] or threads[:4]
    tn = {n: unit_time_any(st, n, ow) for n in ns}
    if not threads or not all(tn.values()):
        return note("  lock_cs_length: no common thread counts or no 1-thread runs, skipped")
    fig, axs, spare = panels(len(threads), ncols=4, height=2.5)
    fl = Flags()
    for ax, p in zip(axs, threads):
        for impl in impls:
            pts = []
            for n in ns:
                s = st.get((impl, p, ow, n))
                if s and s.valid and s.iters:
                    pts.append((ns.index(n),) + s.triple(1.0 / bound(s.iters, tn[n], p, ow)))
            fl.add(draw(ax, pts, sty.get("lock", impl)))
        ax.axhline(1.0, color=BLACK, lw=1.0, ls=(0, (4, 2)), zorder=1.5)
        ax.set_xticks(range(len(ns)))
        ax.set_xticklabels(["%d\n%s" % (n, si_time(tn[n], False)) for n in ns])
        ax.set_xlim(-0.4, len(ns) - 0.6)
        log_y(ax)
        ax.set_title("%d thread" % p, fontsize=9.5)
    handles = [proxy(sty.get("lock", i), i) for i in impls]
    handles.append(Line2D([], [], color=BLACK, lw=1.0, ls=(0, (4, 2)), label="cận dưới lý tưởng (= 1)"))
    legend(fig, spare, handles + fl.handles(), ncol=4)
    fig.supxlabel("N và thời gian một lời gọi is_prime(N)", fontsize=10)
    supylabel(fig, "Thời gian đo / cận dưới lý tưởng", fontsize=10)
    tighten_log_y(axs)
    save(fig, host, out, "lock_cs_length", host.synthetic)
    cap["lock_cs_length"] = (
        synthetic_prefix(host) +
        r"Thời gian đo chia cho cận dưới lý tưởng theo độ dài critical section, \texttt{OUTSIDE\_WORK} = %d, mỗi ô "
        r"một số thread. Dưới mỗi $N$ là thời gian của một lời gọi \texttt{is\_prime(N)}." % ow
)


def variant_figure(host, sty, part, bases, ows, n, name, out):
    """Rows: base implementations; columns: OUTSIDE_WORK; y: time(variant) / median time(base),
    both per iteration. Returns the Flags of the figure, or None when there is nothing to draw."""
    st = host.stats[part]
    impls = host.impls(part)
    rows = [(b, [i for i in sty.order(part, impls) if sty.split(part, i)[0] == b and i != b]) for b in bases]
    rows = [(b, v) for b, v in rows if v and b in impls]
    if not rows or not ows:
        return None
    threads = sorted({s.threads for s in st.values() if s.n == n and s.ow in ows})
    fig, axs = plt.subplots(len(rows), len(ows), figsize=(FIG_WIDTH, 1.9 * len(rows) + 0.5),
                            sharex=True, sharey="row", squeeze=False)
    handles, fl = [], Flags()
    for r, (base, variants) in enumerate(rows):
        for c, ow in enumerate(ows):
            ax = axs[r][c]
            for v in variants:
                pts = []
                for p in threads:
                    s, b = st.get((v, p, ow, n)), st.get((base, p, ow, n))
                    if s and b and s.valid and b.sec and not b.censored and s.iters:
                        q = s.triple(b.iters / s.iters / b.med)
                        pts.append((p,) + q[:4] + (q[4] or b.reduced,))
                fl.add(draw(ax, pts, sty.get(part, v)))
            ax.axhline(1.0, color=sty.get(part, base)["color"], lw=1.1, zorder=1.5)
            thread_axis(ax, threads, host)
            log_y(ax)
            lo, hi = ax.get_ylim()                     # never zoom into the noise around 1
            ax.set_ylim(min(lo, 0.5), max(hi, 2.0))
            if r == 0:
                ax.set_title("OUTSIDE_WORK = %d" % ow, fontsize=9.5)
            if c == 0:
                ax.set_ylabel("so với %s" % base, fontsize=9.5)
        handles += [Line2D([], [], color=sty.get(part, base)["color"], lw=1.1, label="%s (= 1)" % base)]
        handles += [proxy(sty.get(part, v), v) for v in variants]
    fig.legend(handles=handles + core_handles(host) + fl.handles(), loc="outside upper center",
               frameon=False, ncol=4, handlelength=3.2)
    fig.supxlabel("Số thread", fontsize=10)
    supylabel(fig, "So với bản gốc (< 1: nhanh hơn)", fontsize=10)
    tighten_log_y(list(axs.flat))
    save(fig, host, out, name, host.synthetic)
    return fl


def pick(values, k=3):
    """First, middle and last of a sorted list (at most k values)."""
    values = sorted(values)
    if len(values) <= k:
        return values
    return [values[0], values[len(values) // 2], values[-1]]


def fig_lock_backoff(host, sty, out, cap):
    st = host.stats["lock"]
    roles = lock_roles(st)
    if not roles:
        return note("  lock_backoff: no data, skipped")
    bases = sty.order("lock", sty.plain("lock", host.impls("lock")))
    for suffix, n in roles.items():
        ows = pick({s.ow for s in st.values() if s.n == n})
        fl = variant_figure(host, sty, "lock", bases, ows, n, "lock_backoff" + suffix, out)
        if fl is None:
            return note("  lock_backoff: no backoff variant found, skipped")
        cap["lock_backoff" + suffix] = (
            synthetic_prefix(host) +
            r"Thời gian của từng biến thể có backoff chia cho lock gốc không backoff (đường ngang ở 1), $N = %d$; "
            r"hàng trên là họ TAS, hàng dưới là họ TTAS. Kiểu đường cho biết chính sách chờ: ngủ bằng "
            r"\texttt{sleep\_for} (\texttt{\_bo\_sleep}), quay (\texttt{\_bo\_spin}), quay rồi nhường core "
            r"(\texttt{\_bo\_yield})." % n
)


def fig_lock_fairness(host, sty, out, cap):
    st = host.stats["lock"]
    n = main_n(st)
    if n is None or not any(s.x.get("fair") for s in st.values()):
        return note("  lock_fairness: the extra field carries no thr_min_s/thr_max_s, skipped")
    impls = sty.order("lock", sty.plain("lock", host.impls("lock")))
    ows = pick({s.ow for s in st.values() if s.n == n})
    threads = sorted({s.threads for s in st.values() if s.n == n and s.threads > 1})
    fig, axs, spare = panels(len(ows), height=2.4)
    fl = Flags()
    for ax, ow in zip(axs, ows):
        for impl in impls:
            pts = [(p,) + st[(impl, p, ow, n)].xtriple("fair") for p in threads
                   if (impl, p, ow, n) in st and st[(impl, p, ow, n)].xtriple("fair")]
            fl.add(draw(ax, pts, sty.get("lock", impl), zero_ok=True))
        thread_axis(ax, threads, host)
        ax.set_ylim(0, 1.05)
        comma_axis(ax)
        ax.set_title("OUTSIDE_WORK = %d" % ow, fontsize=9.5)
    legend(fig, spare, [proxy(sty.get("lock", i), i) for i in impls] + core_handles(host) + fl.handles(), ncol=5)
    fig.supxlabel("Số thread", fontsize=10)
    supylabel(fig, "Độ công bằng", fontsize=10)
    save(fig, host, out, "lock_fairness", host.synthetic)
    cap["lock_fairness"] = (
        synthetic_prefix(host) +
        r"Độ công bằng, $N = %d$: thời gian của thread xong sớm nhất chia cho thread xong muộn nhất trong cùng một "
        r"lần chạy (1 là hoàn toàn đều). Cấu hình có trung vị vượt thời hạn không được vẽ." % n
)


def fig_lock_relax(host, sty, out, cap):
    """The build with the other spin-wait hint (lock_yield.csv) relative to the default build."""
    st, sy = host.stats["lock"], host.stats["lock_yield"]
    if not sy:
        return note("  lock_relax: no lock_yield.csv, skipped")
    hint = ", ".join(sorted({r["extra"].get("relax", "?") for r in host.rows["lock_yield"] if r["extra"]})) or "?"
    impls = [i for i in sty.order("lock", host.impls("lock_yield")) if i in host.impls("lock")]
    n = main_n(sy)
    ows = sorted({s.ow for s in sy.values() if s.n == n})
    threads = sorted({s.threads for s in sy.values() if s.n == n})
    if not impls or not ows:
        return note("  lock_relax: no lock in common with lock.csv, skipped")
    fig, axs, spare = panels(len(ows), height=2.6)
    fl = Flags()
    for ax, ow in zip(axs, ows):
        for impl in impls:
            pts = []
            for p in threads:
                s, b = sy.get((impl, p, ow, n)), st.get((impl, p, ow, n))
                if s and b and s.valid and b.sec and not b.censored and s.iters:
                    q = s.triple(b.iters / s.iters / b.med)
                    pts.append((p,) + q[:4] + (q[4] or b.reduced,))
            fl.add(draw(ax, pts, sty.get("lock", impl)))
        ax.axhline(1.0, color=BLACK, lw=1.0, ls=(0, (4, 2)), zorder=1.5)
        thread_axis(ax, threads, host)
        log_y(ax)
        lo, hi = ax.get_ylim()
        ax.set_ylim(min(lo, 0.5), max(hi, 2.0))
        ax.set_title("OUTSIDE_WORK = %d" % ow, fontsize=9.5)
    handles = [proxy(sty.get("lock", i), i) for i in impls]
    handles.append(Line2D([], [], color=BLACK, lw=1.0, ls=(0, (4, 2)), label="bản mặc định (= 1)"))
    legend(fig, spare, handles + core_handles(host) + fl.handles(), ncol=4)
    fig.supxlabel("Số thread", fontsize=10)
    supylabel(fig, "Thời gian với lệnh %s / thời gian với lệnh %s" % (hint, host.relax or "mặc định"), fontsize=10)
    tighten_log_y(axs)
    save(fig, host, out, "lock_relax", host.synthetic)
    cap["lock_relax"] = (
        synthetic_prefix(host) +
        r"Ảnh hưởng của lệnh chờ trong vòng spin, $N = %d$: thời gian chạy của bản biên dịch dùng lệnh \texttt{%s} "
        r"(\texttt{-DPC\_RELAX\_YIELD}) chia cho trung vị của bản mặc định dùng \texttt{%s}, cùng số vòng lặp. "
        r"Nhỏ hơn 1: bản \texttt{%s} nhanh hơn. %s%s%s"
        % (n, tex(hint), tex(host.relax or "?"), tex(hint), stat_note(host.reps("lock_yield")), cores_note(host),
           fl.caption()))


# ---- figures: barrier -----------------------------------------------------------------------------------------
def fig_barrier(host, sty, out, cap):
    st = host.stats["barrier"]
    impls = sty.order("barrier", host.impls("barrier"))
    if not st or not impls:
        return note("  barrier_rate: no data, skipped")
    threads = sorted({s.threads for s in st.values()})
    fig, ax = plt.subplots(figsize=(FIG_WIDTH * 0.8, 3.5))
    skipped, fl = False, Flags()
    for impl in impls:
        pts, miss = [], []
        for p in threads:
            s = next((x for k, x in st.items() if k[0] == impl and k[1] == p), None)
            if s and s.rate and not s.censored:
                pts.append((p, float(np.median(s.rate)), min(s.rate), max(s.rate), False, False))
            elif s and s.censored:
                miss.append(p)
        fl.add(draw(ax, pts, sty.get("barrier", impl), label=impl))
        skipped |= mark_missing(ax, miss, sty.get("barrier", impl))
    thread_axis(ax, threads, host)
    log_y(ax)
    ax.set_xlabel("Số thread")
    ax.set_ylabel("Số episode mỗi giây")
    fig.legend(handles=[proxy(sty.get("barrier", i), i) for i in impls] + core_handles(host)
               + ([Line2D([], [], **MISSING_HANDLE)] if skipped else []),
               loc="outside right center", frameon=False, handlelength=3.2)
    save(fig, host, out, "barrier_rate", host.synthetic)
    cap["barrier_rate"] = (
        synthetic_prefix(host) +
        r"Số barrier episode mỗi giây theo số thread, không có việc giữa hai barrier (trục tung logarit). "
        r"\texttt{sr} chờ bằng vòng spin, \texttt{sr\_bo\_*} thêm backoff, \texttt{sr\_block} chờ chặn.%s"
        % (MISSING_NOTE if skipped else "")
)


# ---- figures: counters -------------------------------------------------------------------------------------------
COUNTER_REF = 1_000_000      # counters are compared through the time of this many increments per thread


def counter_n(st):
    c = collections.Counter(s.n for s in st.values())
    return c.most_common(1)[0][0] if c else None


def fig_counter_time(host, sty, out, cap):
    st = host.stats["counter"]
    n = counter_n(st)
    impls = sty.order("counter", sty.plain("counter", host.impls("counter")))
    if n is None or not impls:
        return note("  counter_time: no data, skipped")
    ows = sorted({s.ow for s in st.values() if s.n == n})
    threads = sorted({s.threads for s in st.values() if s.n == n})
    fig, axs, spare = panels(len(ows), height=2.6)
    fl = Flags()
    for ax, ow in zip(axs, ows):
        for impl in impls:
            pts = [(p,) + st[(impl, p, ow, n)].triple(COUNTER_REF / st[(impl, p, ow, n)].iters) for p in threads
                   if (impl, p, ow, n) in st and st[(impl, p, ow, n)].valid and st[(impl, p, ow, n)].iters]
            fl.add(draw(ax, pts, sty.get("counter", impl)))
        thread_axis(ax, threads, host)
        log_y(ax)
        ax.set_title("OUTSIDE_WORK = %d" % ow, fontsize=9.5)
    fl.red = False                                            # every counter run is scaled; no ring needed
    legend(fig, spare, [proxy(sty.get("counter", i), i) for i in impls] + core_handles(host) + fl.handles(), ncol=5)
    fig.supxlabel("Số thread", fontsize=10)
    supylabel(fig, "Thời gian cho $10^6$ lần tăng (s)", fontsize=10)
    tighten_log_y(axs)
    save(fig, host, out, "counter_time", host.synthetic)
    cap["counter_time"] = (
        synthetic_prefix(host) +
        r"Thời gian chạy quy về $10^6$ lần tăng mỗi thread, mỗi ô một lượng việc \texttt{OUTSIDE\_WORK} (lời gọi "
        r"\texttt{is\_prime(%d)}) giữa hai lần tăng. Các biến thể có backoff và \texttt{seq\_cst} chỉ có trong "
        r"bảng thời gian của counter." % n
)


def fig_counter_variants(host, sty, out, cap):
    st = host.stats["counter"]
    n = counter_n(st)
    if n is None:
        return note("  counter_variants: no data, skipped")
    ows = pick({s.ow for s in st.values() if s.n == n})
    impls = host.impls("counter")
    bases = []
    for i in sty.order("counter", impls):
        b = sty.split("counter", i)[0]
        if b != i and b not in bases:
            bases.append(b)
    fl = variant_figure(host, sty, "counter", bases, ows, n, "counter_variants", out)
    if fl is None:
        return note("  counter_variants: no variant found, skipped")
    fl.red = False
    cap["counter_variants"] = (
        synthetic_prefix(host) +
        r"Biến thể của từng counter so với bản gốc (đường ngang ở 1): thời gian mỗi lần tăng của biến thể chia cho "
        r"trung vị của bản gốc. Kiểu đường và kiểu tô của điểm cho biết biến thể: backoff ngủ, backoff quay, quay rồi "
        r"nhường core, hoặc memory order \texttt{seq\_cst} thay cho thứ tự yếu hơn. %s%s%s"
        % (stat_note(host.reps("counter")), cores_note(host), fl.caption()))


def fig_counter_cas(host, sty, out, cap):
    st = host.stats["counter"]
    n = counter_n(st)
    impls = [i for i in sty.order("counter", host.impls("counter"))
             if any(s.x.get("cas_fail") for k, s in st.items() if k[0] == i)]
    if n is None or not impls:
        return note("  counter_cas_failures: the extra field carries no cas_fail, skipped")
    ows = sorted({s.ow for s in st.values() if s.n == n})
    threads = sorted({s.threads for s in st.values() if s.n == n})
    fig, axs, spare = panels(len(ows), height=2.5, sharey=False)
    # the CAS variants share one colour in the counter style; give each its own colour here
    palette = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#000000"]
    style = {}
    for k, i in enumerate(impls):
        c, st_i = palette[k % len(palette)], dict(sty.get("counter", i))
        st_i.update(color=c, markeredgecolor=c)
        if st_i["markerfacecolor"] != "white":
            st_i["markerfacecolor"] = c
        style[i] = st_i
    for ax, ow in zip(axs, ows):
        for impl in impls:
            pts = [(p,) + st[(impl, p, ow, n)].xtriple("cas_fail")[:4] + (False,) for p in threads
                   if (impl, p, ow, n) in st and st[(impl, p, ow, n)].xtriple("cas_fail")]
            draw(ax, pts, style[impl], zero_ok=True)
        thread_axis(ax, threads, host)
        ax.set_yscale("symlog", linthresh=0.01)
        ax.set_ylim(bottom=0)
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, p: vn_plain(round(v, 6))))
        ax.yaxis.set_minor_locator(ticker.NullLocator())
        ax.set_title("OUTSIDE_WORK = %d" % ow, fontsize=9.5)
    legend(fig, spare, [proxy(style[i], i) for i in impls] + core_handles(host), ncol=5)
    fig.supxlabel("Số thread", fontsize=10)
    supylabel(fig, "Số lần CAS hỏng mỗi lần tăng", fontsize=10)
    save(fig, host, out, "counter_cas_failures", host.synthetic)
    cap["counter_cas_failures"] = (
        synthetic_prefix(host) +
        r"Số lần compare-and-swap hỏng trên mỗi lần tăng thành công của các counter dùng CAS. Trục tung tuyến tính "
        r"dưới 0,01 và logarit phía trên; 0 nghĩa là không lần thử nào hỏng."
)


# ---- figures: cross-machine -----------------------------------------------------------------------------------------
def fig_cross(hosts, sty, out, cap):
    synthetic = any(h.synthetic for h in hosts)

    def frame(part, draw_host, ylabel, name):
        hs = [h for h in hosts if h.stats[part]]
        if len(hs) < 2:
            return None
        fig, axs, _ = panels(len(hs), ncols=min(3, len(hs)), height=3.1)
        used, fl = [], Flags()
        for ax, h in zip(axs, hs):
            for impl in draw_host(ax, h, fl):
                if impl not in used:
                    used.append(impl)
            log_y(ax)
            cpu = re.sub(r"^\d+th Gen Intel\(R\) Core\(TM\) ", "", h.machine.get("cpu_model", h.arch))
            unit = "luồng phần cứng" if "luồng" in ncpu_label(h) else "core"
            ax.set_title("%s\n%s, %d %s" % (h.name, cpu, h.ncpu, unit), fontsize=9)
        extra = [Line2D([], [], color="0.35", lw=0.9, label="số luồng phần cứng của từng máy"),
                 Line2D([], [], color="0.35", lw=0.9, ls=(0, (1, 2)), label="số P-core, hoặc số core máy ảo báo")]
        if part == "lock":
            extra.insert(0, Line2D([], [], color=BLACK, lw=1.0, ls=(0, (4, 2)), label="cận dưới lý tưởng (= 1)"))
        if part == "counter":
            fl.red = False
        fig.legend(handles=[proxy(sty.get(part, i), i) for i in sty.order(part, used)] + extra + fl.handles(),
                   loc="outside upper center", frameon=False, ncol=4, handlelength=2.6)
        fig.supxlabel("Số thread", fontsize=10)
        supylabel(fig, ylabel, fontsize=10)
        tighten_log_y(axs)
        save(fig, None, out, name, synthetic)
        return fl

    pre = r"\textbf{[DỮ LIỆU GIẢ để thử công cụ]} " if synthetic else ""
    marks = " Vạch dọc liền: số luồng phần cứng của từng máy (bằng số core nếu không có SMT); vạch chấm: số P-core hoặc số core vật lý."
    # locks: slowdown over the bound at each machine's main N and the highest common contention
    sets = [{s.ow for s in h.stats["lock"].values() if s.n == main_n(h.stats["lock"])} for h in hosts if h.stats["lock"]]
    common = set.intersection(*sets) if sets else set()
    common_ow = min(common) if common else None

    def lock_host(ax, h, fl):
        st, n = h.stats["lock"], main_n(h.stats["lock"])
        t = unit_time_any(st, n, common_ow)
        impls = sty.order("lock", sty.plain("lock", h.impls("lock")))
        threads = sorted({s.threads for s in st.values() if s.n == n and s.ow == common_ow})
        for impl in impls:
            pts = []
            for p in threads:
                s = st.get((impl, p, common_ow, n))
                if s and s.valid and s.iters and t:
                    pts.append((p,) + s.triple(1.0 / bound(s.iters, t, p, common_ow)))
            fl.add(draw(ax, pts, sty.get("lock", impl)))
        ax.axhline(1.0, color=BLACK, lw=1.0, ls=(0, (4, 2)), zorder=1.5)
        if threads:
            thread_axis(ax, threads, h)
        return impls

    if common_ow is not None:
        fl = frame("lock", lock_host, "Thời gian đo / cận dưới lý tưởng", "lock")
        if fl:
            cap["lock"] = (pre + r"So sánh các máy, phần lock: thời gian đo chia cho cận dưới lý tưởng của chính máy đó, "
                           r"\texttt{OUTSIDE\_WORK} = %d, cùng thang đo. Chia cho cận dưới khử khác biệt về tốc độ core." % common_ow)

    def barrier_host(ax, h, fl):
        st = h.stats["barrier"]
        impls = sty.order("barrier", h.impls("barrier"))
        threads = sorted({s.threads for s in st.values()})
        for impl in impls:
            pts = [(k[1], float(np.median(s.rate)), min(s.rate), max(s.rate), False, False)
                   for k, s in sorted(st.items(), key=lambda kv: kv[0][1]) if k[0] == impl and s.rate and not s.censored]
            draw(ax, pts, sty.get("barrier", impl))
        if threads:
            thread_axis(ax, threads, h)
        return impls

    if frame("barrier", barrier_host, "Số episode mỗi giây", "barrier"):
        cap["barrier"] = (pre + r"So sánh các máy, phần barrier: số episode mỗi giây theo số thread, cùng thang đo.")

    def counter_host(ax, h, fl):
        st, n = h.stats["counter"], counter_n(h.stats["counter"])
        ow = min({s.ow for s in st.values() if s.n == n})
        impls = sty.order("counter", sty.plain("counter", h.impls("counter")))
        threads = sorted({s.threads for s in st.values() if s.n == n and s.ow == ow})
        for impl in impls:
            pts = [(p,) + st[(impl, p, ow, n)].triple(COUNTER_REF / st[(impl, p, ow, n)].iters) for p in threads
                   if (impl, p, ow, n) in st and st[(impl, p, ow, n)].valid and st[(impl, p, ow, n)].iters]
            fl.add(draw(ax, pts, sty.get("counter", impl)))
        if threads:
            thread_axis(ax, threads, h)
        return impls

    fl = frame("counter", counter_host, "Thời gian cho $10^6$ lần tăng (s)", "counter")
    if fl:
        cap["counter"] = (pre + r"So sánh các máy, phần counter ở mức tranh chấp cao nhất (\texttt{OUTSIDE\_WORK} nhỏ nhất của "
                          r"từng máy): thời gian chạy quy về $10^6$ lần tăng mỗi thread, cùng thang đo.")


# ---- tables -----------------------------------------------------------------------------------------------------------
PINNING_VN = [("not available", "không có (macOS trên Apple Silicon không có API ghim thread)"),
              ("available", r"có (\texttt{sched\_setaffinity}) nhưng các benchmark không dùng")]
POWER_VN = {"AC Power": "cắm sạc", "Battery Power": "pin"}


def vn_size(v):
    """'48K' -> '48 KiB', '1280K' -> '1,25 MiB', '7.607 GiB' -> '7,6 GiB', 'per 6 P cores' -> 'cho 6 P-core'."""
    if not v:
        return "–"
    m = re.fullmatch(r"(\d+)K", v.strip())
    if m:
        k = int(m.group(1))
        if k >= 1024:
            mib = k / 1024
            return ("%g MiB" % round(mib, 2)).replace(".", ",")
        return "%d KiB" % k
    v = re.sub(r"(\d+)\.(\d+) GiB", lambda g: ("%g GiB" % round(float(g.group(0)[:-4]), 1)).replace(".", ","), v)
    v = re.sub(r"per (\d+) ([PE]) cores", r"cho \1 \2-core", v)
    return v


def power_note(h):
    """Power source, flagged when machine.txt was written after the campaign ended."""
    m = h.machine
    text = POWER_VN.get(m.get("power", ""), m.get("power", "–"))
    try:
        end = None
        for line in open(os.path.join(h.dir, "campaign.log")):
            if " END " in line:
                end = line.split()[0]
        written = m.get("date", "")[:19].replace(" ", "T")
        if end and written and written > end[:19]:
            text += " (ghi sau đợt đo)"
    except OSError:
        pass
    return text


def machine_rows(h):
    m = h.machine
    cores = str(h.ncpu or m.get("logical_cpus", "–"))
    if m.get("performance_cores") and m.get("efficiency_cores"):
        cores += " (%s P-core + %s E-core)" % (m["performance_cores"], m["efficiency_cores"])
    elif m.get("physical_cores") and m.get("physical_cores") != m.get("logical_cpus"):
        cores = "%s luồng phần cứng trên %s core vật lý%s" % (m.get("logical_cpus", cores), m["physical_cores"],
                                                              " (máy ảo báo)" if m.get("virtualization") else "")
    pin = m.get("thread_pinning", "")
    for key, text in PINNING_VN:
        if pin.startswith(key):
            pin = text
            break
    else:
        pin = tex(pin)
    cl = m.get("cache_line_bytes", "")
    used = m.get("cache_line_used_by_code", "")
    return [
        ("CPU", tex(m.get("cpu_model", "–"))),
        ("Kiến trúc", tex(h.arch or m.get("arch", "–"))),
        ("Số core", tex(cores)),
        ("Cache line (hệ thống báo)", tex(cl + " B") if cl else "–"),
        ("Cache line dùng để đệm trong mã", tex(used + " B") if used else "–"),
        ("Cache L1d", tex(vn_size(m.get("l1d_cache", "")))),
        ("Cache L2", tex(vn_size(m.get("l2_cache", "")))),
        ("Cache L3", tex(vn_size(m.get("l3_cache", "")))),
        ("RAM", tex(vn_size(m.get("ram", "")))),
        ("Hệ điều hành", tex(m.get("os", h.os or "–"))),
        ("Nhân", tex(m.get("kernel", "–"))),
        ("Compiler", tex(m.get("compiler", h.compiler or "–"))),
        ("Cờ biên dịch", tt(m["cxxflags"]) if m.get("cxxflags") else "–"),
        ("OpenMP", {"yes": "có", "no": r"không (không có baseline OpenMP)"}.get(m.get("openmp", ""), "–")),
        ("Lệnh chờ trong vòng spin", tt(h.relax) if h.relax else "–"),
        ("Ghim thread vào core", pin or "–"),
        ("Nguồn điện lúc đo", tex(power_note(h))),
        ("Thành viên chạy đo", r"\PCowner{%s}" % h.name),
    ]


def machine_table(hosts):
    rows = [machine_rows(h) for h in hosts]
    width = (15.2 - 4.4 - 0.45 * len(hosts)) / len(hosts)
    out = [r"\begingroup\footnotesize",
           r"\begin{tabular}{@{}>{\raggedright\arraybackslash}p{4.4cm}%s@{}}"
           % ("".join(r">{\raggedright\arraybackslash}p{%.2fcm}" % width for _ in hosts)),
           r"\toprule",
           " & ".join([""] + [r"\textbf{%s}" % tt(h.name) for h in hosts]) + r" \\", r"\midrule"]
    for i, (label, _) in enumerate(rows[0]):
        out.append(" & ".join([label] + [r[i][1] for r in rows]) + r" \\")
    out += [r"\bottomrule", r"\end{tabular}", r"\endgroup"]
    return "\n".join(out)


def grid_table(row_names, col_names, cell, corner, extra_rows=()):
    """Rows = implementations, columns = thread counts."""
    out = [r"\begingroup\footnotesize\setlength{\tabcolsep}{3.5pt}",
           r"\begin{tabular}{@{}l%s@{}}" % ("r" * len(col_names)), r"\toprule",
           " & ".join([corner] + [str(c) for c in col_names]) + r" \\", r"\midrule"]
    for name in row_names:
        out.append(" & ".join([tt(name)] + [cell(name, c) for c in col_names]) + r" \\")
    if extra_rows:
        out.append(r"\midrule")
        for label, fn in extra_rows:
            out.append(" & ".join([label] + [fn(c) for c in col_names]) + r" \\")
    out += [r"\bottomrule", r"\end{tabular}", r"\endgroup"]
    body = "\n".join(out)
    if len(col_names) > 11:
        body = r"\resizebox{\linewidth}{!}{%" + "\n" + body + "}"
    return body


def time_cell(s, ref):
    """Median time scaled to `ref` iterations per thread; > for a timeout, a dagger for a reduced run."""
    if s is None or not s.valid or not s.iters:
        return "–"
    mark = r"$^{\dagger}$" if s.reduced else ""
    if s.censored:
        return r"$>$\,%s%s" % (vn(s.cap * ref / s.iters, 2, True), mark)
    return vn(s.med * ref / s.iters, 3, True) + mark


DAGGER_NOTE = (r" $\dagger$: đo với số vòng lặp nhỏ hơn 100 lần (cấu hình sụp đổ hoặc đã vượt thời hạn), "
               r"rồi quy đổi tuyến tính.")


def campaign_table(h):
    parts = ["lock", "barrier", "counter"] + (["lock_yield"] if h.rows["lock_yield"] else [])
    titles = {"lock": "Lock", "barrier": "Barrier", "counter": "Counter", "lock_yield": r"Lock (\texttt{yield})"}
    out = [r"\begingroup\footnotesize\setlength{\tabcolsep}{5pt}",
           r"\begin{tabular}{@{}l%s@{}}" % ("r" * len(parts)), r"\toprule",
           " & ".join([""] + [titles[p] for p in parts]) + r" \\", r"\midrule"]

    def line(label, fn):
        out.append(" & ".join([label] + [fn(p) for p in parts]) + r" \\")
    rows = h.rows
    line("Số cài đặt", lambda p: str(len(h.impls(p))) if rows[p] else "–")
    line("Số cấu hình", lambda p: str(len(h.stats[p])) if rows[p] else "–")
    line("Số lần lặp mỗi cấu hình", lambda p: str(h.reps(p)) if rows[p] else "–")
    line("Số lần chạy đã ghi", lambda p: vint(len(rows[p]), True) if rows[p] else "–")
    line("Cấu hình đo với số vòng lặp giảm",
         lambda p: str(sum(1 for s in h.stats[p].values() if s.reduced)) if rows[p] and p != "counter" else "–")
    line("Lần chạy vượt thời hạn", lambda p: str(sum(1 for r in rows[p] if r["extra_raw"] == "timeout")) if rows[p] else "–")
    line("Lần chạy bỏ qua sau khi vượt thời hạn",
         lambda p: str(sum(1 for r in rows[p] if r["extra_raw"] == "timeout_skipped")) if rows[p] else "–")
    line(r"Lần chạy hỏng phép kiểm (\texttt{ok=0})",
         lambda p: str(sum(1 for r in rows[p] if r["ok"] != 1 and not r["extra_raw"].startswith("timeout"))) if rows[p] else "–")
    line("Load average 1 phút (nhỏ nhất – lớn nhất)",
         lambda p: "%s – %s" % (vn(min(r["load1"] for r in rows[p]), 3, True), vn(max(r["load1"] for r in rows[p]), 3, True)) if rows[p] else "–")
    def stamp(t):                                           # 2026-10-14T09:49:12 -> 14/10 09:49
        m = re.match(r"\d+-(\d+)-(\d+)T(\d+:\d+)", t)
        return "%s/%s %s" % (m.group(2), m.group(1), m.group(3)) if m else tex(t)
    line("Bắt đầu (ngày/tháng giờ:phút)", lambda p: stamp(min(r["timestamp"] for r in rows[p])) if rows[p] else "–")
    line("Kết thúc", lambda p: stamp(max(r["timestamp"] for r in rows[p])) if rows[p] else "–")
    out += [r"\bottomrule", r"\end{tabular}", r"\endgroup"]
    return "\n".join(out)


def sleep_table(h):
    path = os.path.join(h.dir, "sleep_latency.csv")
    if not os.path.exists(path):
        return None
    with open(path, newline="") as f:
        rows = [r for r in csv.reader(f) if r]
    if len(rows) < 2:
        return None
    names = {"what": "Phép chờ", "kind": "Phép chờ", "requested_ns": "Yêu cầu (ns)", "samples": "Số mẫu",
             "reps": "Số mẫu", "min_ns": "Nhỏ nhất (ns)", "median_ns": "Trung vị (ns)",
             "p99_ns": "Phân vị 99 (ns)", "max_ns": "Lớn nhất (ns)"}
    keep = [i for i, c in enumerate(rows[0]) if c not in ("host", "os", "arch")]
    out = [r"\begingroup\footnotesize", r"\begin{tabular}{@{}l%s@{}}" % ("r" * (len(keep) - 1)), r"\toprule",
           " & ".join(names.get(rows[0][i], tex(rows[0][i])) for i in keep) + r" \\", r"\midrule"]
    for r in rows[1:]:
        cells = []
        for k, i in enumerate(keep):
            v = r[i] if i < len(r) else ""
            try:
                cells.append(tt(v) if k == 0 else vn(float(v), 3, True))
            except ValueError:
                cells.append(tex(v))
        out.append(" & ".join(cells) + r" \\")
    out += [r"\bottomrule", r"\end{tabular}", r"\endgroup"]
    return "\n".join(out)


def host_tables(h, sty):
    """key -> (caption, body) for one machine."""
    tabs, pre = collections.OrderedDict(), synthetic_prefix(h)
    appendix = []
    tabs["machine"] = (pre + "Cấu hình của máy %s." % tt(h.name), machine_table([h]))
    tabs["campaign"] = (pre + "Tóm tắt đợt đo trên máy %s: số lần chạy, số lần vượt thời hạn và tải nền ghi trong "
                        "từng dòng số liệu." % tt(h.name), campaign_table(h))
    st = h.stats["lock"]
    if st:
        n = main_n(st)
        impls = sty.order("lock", h.impls("lock"))
        # the work unit: t for every (N, OUTSIDE_WORK) -- the linearity check
        combos = sorted({(s.n, s.ow) for s in st.values()})
        out = [r"\begingroup\footnotesize", r"\begin{tabular}{@{}rrrrr@{}}", r"\toprule",
               r"$N$ & \texttt{OUTSIDE\_WORK} & $I$ (vòng lặp mỗi thread) & Thời gian 1 thread (s) & $t$ (ns) \\", r"\midrule"]
        for nn, ow in combos:
            t = unit_time(st, nn, ow)
            out.append(" & ".join([str(nn), str(ow), vint(ref_iters(st, nn, ow), True), vn(_t1(st, nn, ow), 3, True),
                                   vn(t * 1e9, 3, True) if t else "–"]) + r" \\")
        out += [r"\bottomrule", r"\end{tabular}", r"\endgroup"]
        tabs["lockunit"] = (
            pre + r"Đơn vị công việc trên máy %s: số vòng lặp $I$ của lần chạy 1 thread, thời gian chạy 1 thread của "
            r"lock nhanh nhất, và $t$ = thời gian đó chia cho số lời gọi \texttt{is\_prime}, $I\,(1+\mathrm{OW})$. Nếu với "
            r"cùng $N$ mà $t$ không đổi theo \texttt{OUTSIDE\_WORK} thì thời gian tăng tuyến tính theo số lời gọi, tức "
            r"phần việc không bị compiler xoá. $t$ còn chứa chi phí lấy và nhả lock chia cho $1+\mathrm{OW}$." % tt(h.name),
            "\n".join(out))
        # single-thread cost per iteration
        ns = sorted({s.n for s in st.values()})

        def single(impl, nn):
            c = sorted((s.ow, s) for s in st.values() if s.impl == impl and s.n == nn and s.threads == 1 and s.sec)
            if not c:
                return "–"
            ow, s = c[0]
            return vn(s.med / s.iters / (1 + ow) * 1e9, 3, True)
        tabs["locksingle"] = (
            pre + r"Chi phí khi không tranh chấp trên máy %s: thời gian chạy 1 thread chia cho số lời gọi "
            r"\texttt{is\_prime} (ns), theo $N$, lấy ở \texttt{OUTSIDE\_WORK} nhỏ nhất có số liệu. Chênh lệch giữa các "
            r"hàng là chênh lệch chi phí lấy và nhả lock khi chỉ có một thread." % tt(h.name),
            grid_table(impls, ns, single, r"Lock \textbackslash{} $N$"))
        threads = sorted({s.threads for s in st.values() if s.n == n})
        for ow in sorted({s.ow for s in st.values() if s.n == n}):
            t, iref = unit_time_any(st, n, ow), ref_iters(st, n, ow)
            body = grid_table(impls, threads, lambda i, p, ow=ow, iref=iref: time_cell(st.get((i, p, ow, n)), iref),
                              r"Lock \textbackslash{} số thread",
                              [("cận dưới lý tưởng", lambda p, ow=ow, t=t, iref=iref: vn(bound(iref, t, p, ow), 3, True) if t else "–")])
            caption = (pre + r"Trung vị thời gian chạy (s) của mọi lock trên máy %s, $N = %d$, \texttt{OUTSIDE\_WORK} = %d, "
                       r"quy về $I = %s$ vòng lặp mỗi thread (thời gian đo nhân với $I/I_{\text{đo}}$). Ký hiệu $>x$: "
                       r"trung vị vượt thời hạn của một lần chạy.%s"
                       % (tt(h.name), n, ow, vint(iref, True), DAGGER_NOTE if h.any_reduced else ""))
            key = "locktime" if "locktime" not in tabs else None
            if key:
                tabs[key] = (caption, body)
            else:
                appendix.append(("locktime-ow%d" % ow, caption, body))
    st = h.stats["barrier"]
    if st:
        impls = sty.order("barrier", h.impls("barrier"))
        threads = sorted({s.threads for s in st.values()})

        def rate(i, p):
            s = next((x for k, x in st.items() if k[0] == i and k[1] == p), None)
            if s is None or not s.valid:
                return "–"
            return "t/h" if s.censored or not s.rate else big(float(np.median(s.rate)))
        tabs["barrier"] = (
            pre + r"Trung vị số barrier episode mỗi giây trên máy %s theo số thread (k = nghìn, M = triệu; "
            r"t/h = vượt thời hạn)." % tt(h.name),
            grid_table(impls, threads, rate, r"Barrier \textbackslash{} số thread"))
    st = h.stats["counter"]
    if st:
        n = counter_n(st)
        impls = sty.order("counter", h.impls("counter"))
        threads = sorted({s.threads for s in st.values() if s.n == n})
        for ow in sorted({s.ow for s in st.values() if s.n == n}):
            body = grid_table(impls, threads,
                              lambda i, p, ow=ow: time_cell(st.get((i, p, ow, n)), COUNTER_REF).replace(r"$^{\dagger}$", ""),
                              r"Counter \textbackslash{} số thread")
            caption = (pre + r"Trung vị thời gian chạy (s) của mọi counter trên máy %s, \texttt{OUTSIDE\_WORK} = %d "
                       r"(lời gọi \texttt{is\_prime(%d)} giữa hai lần tăng), quy về $10^6$ lần tăng mỗi thread "
                       r"(thời gian đo nhân với $10^6/I$; $I$ được hiệu chỉnh riêng cho từng cấu hình). "
                       r"Ký hiệu $>x$: trung vị vượt thời hạn của một lần chạy." % (tt(h.name), ow, n))
            if "countertime" not in tabs:
                tabs["countertime"] = (caption, body)
            else:
                appendix.append(("countertime-ow%d" % ow, caption, body))
        cas = [i for i in impls if any(s.x.get("cas_fail") for k, s in st.items() if k[0] == i)]
        if cas:
            ow = min(s.ow for s in st.values() if s.n == n)

            def fails(i, p):
                s = st.get((i, p, ow, n))
                v = s.xtriple("cas_fail") if s else None
                return vn(v[0], 3, True) if v else "–"
            tabs["casfail"] = (
                pre + r"Trung vị số lần compare-and-swap hỏng trên mỗi lần tăng, máy %s, \texttt{OUTSIDE\_WORK} = %d."
                % (tt(h.name), ow), grid_table(cas, threads, fails, r"Counter \textbackslash{} số thread"))
    body = sleep_table(h)
    if body:
        tabs["sleeplatency"] = (
            pre + r"Thời gian thật của các phép chờ trên máy %s, đo bằng \texttt{build/sleep\_latency} với một thread: "
            r"thời gian yêu cầu và thời gian đo được của \texttt{sleep\_for}, \texttt{yield} và lệnh chờ trong vòng spin."
            % tt(h.name), body)
    return tabs, appendix


def write_host_tex(h, sty, captions, out_dir):
    tabs, appendix = host_tables(h, sty)
    L = ["% Generated by scripts/plot.py from results/" + h.name + "/ -- do not edit, run `make figures`.",
         "% Every table, every figure caption and a few numbers of this machine, as macros:",
         "%   \\PCtable{<host>}{<key>}, \\PCfigure{<host>}{<name>}, \\PCval{<host>}{<key>} (defined in report.tex)."]

    def define(kind, key, body):
        L.append(r"\expandafter\gdef\csname PC@%s@%s@%s\endcsname{%%" % (h.name, kind, key))
        L.append(body + "%")
        L.append("}")
    for key, (caption, body) in tabs.items():
        define("tabcap", key, caption)
        define("tab", key, body)
    floats = []
    for key, caption, body in appendix:
        floats.append("\\begin{table}[H]\\centering\n\\caption{%s}\\label{tab:%s:%s}\n%s\n\\end{table}"
                      % (caption, h.name, key, body))
    define("tab", "appendix", "\n".join(floats) if floats else r"\relax")
    for name, caption in captions.items():
        define("figcap", name, caption)
    m = h.machine
    vals = {"ncpu": str(h.ncpu), "pcores": m.get("performance_cores", ""), "cpu": tex(m.get("cpu_model", "")),
            "os": tex(m.get("os", h.os)), "compiler": tex(m.get("compiler", h.compiler)),
            "arch": tex(h.arch), "name": tt(h.name), "relax": tt(h.relax) if h.relax else "",
            "repslock": str(h.reps("lock")), "repsbarrier": str(h.reps("barrier")),
            "repscounter": str(h.reps("counter")),
            "nlocks": str(len(h.impls("lock"))), "nbarriers": str(len(h.impls("barrier"))),
            "ncounters": str(len(h.impls("counter"))),
            "synthetic": "1" if h.synthetic else "0"}
    for k, v in vals.items():
        define("val", k, v)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "tables.tex")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    note("  wrote %s (%d tables, %d appendix tables, %d figure captions)"
         % (shown(path), len(tabs), len(appendix), len(captions)))


def write_cross_tex(hosts, captions, out_dir):
    pre = r"\textbf{[DỮ LIỆU GIẢ để thử công cụ]} " if any(h.synthetic for h in hosts) else ""
    L = ["% Generated by scripts/plot.py -- do not edit, run `make figures`.",
         "% Machines found in results/: " + ", ".join(h.name for h in hosts)]

    def define(kind, key, body):
        L.append(r"\expandafter\gdef\csname PC@cross@%s@%s\endcsname{%%" % (kind, key))
        L.append(body + "%")
        L.append("}")
    define("tabcap", "machines", pre + "Cấu hình các máy dùng để đo. Thông tin do \\texttt{scripts/machine\\_info.sh} "
           "ghi lại lúc chạy đợt đo; kích thước cache line do hệ điều hành báo.")
    define("tab", "machines", machine_table(hosts))
    for name, caption in captions.items():
        define("figcap", name, caption)
    define("val", "hosts", ",".join(h.name for h in hosts))
    define("val", "nhosts", str(len(hosts)))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "tables.tex")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    note("  wrote %s" % shown(path))


# ---- main ------------------------------------------------------------------------------------------------------------------
HOST_FIGURES = [fig_lock_time, fig_lock_throughput, fig_lock_slowdown, fig_lock_cs_length, fig_lock_backoff,
                fig_lock_fairness, fig_lock_relax, fig_barrier, fig_counter_time, fig_counter_variants,
                fig_counter_cas]


def main():
    ap = argparse.ArgumentParser(description="Draw the figures and write the tables of the report.")
    ap.add_argument("--results", default=os.path.join(ROOT, "results"), help="directory with one sub-directory per machine")
    ap.add_argument("--figures", default=os.path.join(ROOT, "report", "figures"), help="output directory")
    ap.add_argument("--host", action="append", help="only this machine (repeatable)")
    args = ap.parse_args()

    if not os.path.isdir(args.results):
        note("no results directory %s: nothing to draw" % args.results)
        return 0
    dirs = sorted((d for d in os.listdir(args.results)
                   if os.path.isdir(os.path.join(args.results, d)) and not d.startswith((".", "old-"))), key=str.lower)
    if args.host:
        dirs = [d for d in dirs if d in args.host]
    hosts = []
    for d in dirs:
        h = Host(os.path.join(args.results, d))
        if h.empty:
            note("%s: no lock.csv / barrier.csv / counter.csv with rows, skipped" % d)
            continue
        hosts.append(h)
    if not hosts:
        note("no machine with results under %s: nothing to draw (run `make bench` first)" % args.results)
        return 0
    if any(h.synthetic for h in hosts) and os.path.realpath(args.figures).startswith(os.path.realpath(ROOT) + os.sep):
        sys.exit("plot: synthetic (FAKE) results must not be drawn into the project; pass --figures <scratch dir>")

    names = {p: sorted({i for h in hosts for i in h.impls(p)}) for p in ("lock", "barrier", "counter")}
    sty = Styles(names)
    for h in hosts:
        note("%s: %s" % (h.name, ", ".join("%d %s rows" % (len(h.rows[p]), p) for p in h.rows)))
        for p in h.rows:
            if not h.rows[p]:
                note("  %s.csv missing or empty: its figures and tables are skipped" % p)
        out = os.path.join(args.figures, h.name)
        captions = collections.OrderedDict()
        for fn in HOST_FIGURES:
            try:
                fn(h, sty, out, captions)
            except Exception as exc:                          # noqa: BLE001 -- one bad figure must not stop the rest
                note("  ! %s failed: %s: %s" % (fn.__name__, type(exc).__name__, exc))
                plt.close("all")
        write_host_tex(h, sty, captions, out)
    cross = os.path.join(args.figures, "cross")
    captions = collections.OrderedDict()
    if len(hosts) > 1:
        note("cross-machine comparison of %s" % ", ".join(h.name for h in hosts))
        try:
            fig_cross(hosts, sty, cross, captions)
        except Exception as exc:                              # noqa: BLE001
            note("  ! fig_cross failed: %s: %s" % (type(exc).__name__, exc))
            plt.close("all")
    else:
        note("only one machine: no cross-machine figure")
        for name in ("lock", "barrier", "counter"):          # stale figures of an earlier run
            stale = os.path.join(cross, name + ".pdf")
            if os.path.exists(stale):
                os.remove(stale)
    write_cross_tex(hosts, captions, cross)
    return 0


if __name__ == "__main__":
    sys.exit(main())
