import pathlib as _pl
_A1 = str(_pl.Path(__file__).resolve().parents[2])
import csv, collections, statistics as S, sys, json
R = _A1 + "/results/apple-m5/"
def load(p):
    return list(csv.DictReader(open(R+p+".csv")))
allrows = {p: load(p) for p in ["lock","barrier","counter","lock_yield"]}
# 1. spread over repetitions: max/median and mean/median for ok configs
for p, rows in allrows.items():
    g = collections.defaultdict(list)
    for r in rows:
        if r["ok"] == "1":
            g[(r["impl"], r["threads"], r["outside_work"], r["n"], r["iters"])].append(float(r["seconds"]))
    rat = []; mm = []; lo=[]
    for k, v in g.items():
        if len(v) < 5: continue
        med = S.median(v)
        rat.append(max(v)/med); mm.append(S.mean(v)/med); lo.append(min(v)/med)
    rat.sort(); mm.sort()
    n = len(rat)
    print(p, "configs", n, "max/med median %.3f p90 %.3f max %.1f; frac max/med>1.5: %.3f >2: %.3f; mean/med>1.1: %.3f; min/med median %.3f" % (
        S.median(rat), rat[int(0.9*n)], rat[-1], sum(x>1.5 for x in rat)/n, sum(x>2 for x in rat)/n, sum(x>1.1 for x in mm)/n, S.median(lo)))
# 2. drift: 1-thread lock configs, time of rep r / median of config
lock = allrows["lock"]
g = collections.defaultdict(dict)
for r in lock:
    if r["ok"]=="1" and r["threads"]=="1":
        g[(r["impl"], r["outside_work"], r["n"])][int(r["rep"])] = float(r["seconds"])
for rep in range(1,8):
    rs = [v[rep]/S.median(v.values()) for v in g.values() if rep in v]
    print("1-thread lock rep", rep, "median ratio to config median %.4f" % S.median(rs), len(rs))
# 3. 1-thread spread N=1009
sp = [max(v.values())/min(v.values()) for k,v in g.items() if k[2]=="1009"]
sp.sort(); print("1-thread N=1009 max/min: median %.3f max %.3f n=%d" % (S.median(sp), sp[-1], len(sp)))
sp = [max(v.values())/min(v.values()) for k,v in g.items()]
sp.sort(); print("1-thread all N max/min: median %.3f p90 %.3f max %.3f n=%d frac>1.2 %.3f" % (S.median(sp), sp[int(.9*len(sp))], sp[-1], len(sp), sum(x>1.2 for x in sp)/len(sp)))
# 4. load1 per rep
for p, rows in allrows.items():
    ls = [float(r["load1"]) for r in rows]
    print(p, "load1 min %.2f median %.2f max %.2f" % (min(ls), S.median(ls), max(ls)))
# load vs threads
th = collections.defaultdict(list)
for r in lock:
    th[int(r["threads"])].append(float(r["load1"]))
print("lock load1 median by threads:", {k: round(S.median(v),1) for k,v in sorted(th.items())})
# 5. run length distribution (seconds) for ok rows
for p, rows in allrows.items():
    s = sorted(float(r["seconds"]) for r in rows if r["ok"]=="1")
    print(p, "seconds: min %.4f p5 %.4f median %.4f p95 %.4f max %.3f" % (s[0], s[int(.05*len(s))], S.median(s), s[int(.95*len(s))], s[-1]))
# 6. calibration stats
c = json.load(open(R+"calibration.json"))
for part in ["barrier","counter"]:
    it = [v["iters"] for k,v in c.items() if k.startswith(part+"|")]
    print(part, "calib entries", len(it), "iters min", min(it), "max", max(it))
    lim = {"barrier":(50,50_000_000),"counter":(100,2_000_000_000)}[part]
    print("  at floor", sum(i==lim[0] for i in it), "at ceil", sum(i==lim[1] for i in it))
    ks=[k for k,v in c.items() if k.startswith(part+"|") and v["iters"]==lim[0]]
    print("  floor keys", ks)
print("reduced keys", [k for k in c if k.startswith("reduced")])
# lock rows with reduced iters (MCS oversub): iters
red = sorted({(r["impl"], r["threads"], r["outside_work"], r["n"], r["iters"]) for r in lock if r["impl"]=="mcs" and int(r["threads"])>10 and r["ok"]=="1"})
print(len(red)); print(red[:30])
# wall_s minus seconds overhead
ov = [float(r["wall_s"])-float(r["seconds"]) for r in lock if r["ok"]=="1"]
print("wall - seconds: median %.4f p95 %.4f" % (S.median(ov), sorted(ov)[int(.95*len(ov))]))
