import pathlib as _pl
_A1 = str(_pl.Path(__file__).resolve().parents[2])
import csv, collections, statistics as S
R = _A1 + "/results/apple-m5/"
lock = list(csv.DictReader(open(R+"lock.csv")))
v = sorted(float(r["seconds"])/(int(r["iters"])*(1+int(r["outside_work"])))*1e9 for r in lock if r["ok"]=="1" and r["threads"]=="1" and r["n"]=="1009")
print(len(v), "min %.0f p10 %.0f p25 %.0f med %.0f p75 %.0f p90 %.0f max %.0f" % (v[0], v[len(v)//10], v[len(v)//4], S.median(v), v[3*len(v)//4], v[9*len(v)//10], v[-1]))
# histogram
import math
b = collections.Counter(int(x//50)*50 for x in v)
for k in sorted(b): print(k, b[k])
cnt = list(csv.DictReader(open(R+"counter.csv")))
g = collections.defaultdict(list)
for r in cnt:
    g[(r["impl"], r["threads"], r["outside_work"])].append((r["iters"], r["ok"], r["extra"][:20]))
for k, x in g.items():
    oks = [a for a in x if a[1]=="1"]
    its = collections.Counter(a[0] for a in oks)
    if len(oks) < 7 or len(its)>1: print(k, x)
