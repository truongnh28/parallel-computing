# Derived numbers for results_barrier/results_counter. Source: results/apple-m5/{barrier,counter}.csv
import csv, statistics as st, sys, collections
import pathlib
A1=sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parents[2])
def load(f):
    return list(csv.DictReader(open(f"{A1}/results/apple-m5/{f}")))
B=load('barrier.csv'); C=load('counter.csv')
print("== BARRIER: median rate, ns/episode, ns/episode/thread, min/max rate, n timeouts")
g=collections.defaultdict(list)
for r in B: g[(r['impl'],int(r['threads']))].append(r)
for (impl,p),rs in sorted(g.items()):
    ok=[float(r['ops_per_s']) for r in rs if r['ok']=='1']
    med=st.median(ok)
    print(f"{impl:12s} p={p:2d} n={len(ok)} med={med:12.1f} ns/ep={1e9/med:12.1f} ns/ep/p={1e9/med/p:10.1f} min={min(ok):12.1f} max={max(ok):12.1f} iters={rs[0]['iters']}")
print("== COUNTER")
g=collections.defaultdict(list)
for r in C: g[(r['impl'],int(r['outside_work']),int(r['threads']))].append(r)
def ex(r):
    d={}
    for kv in r['extra'].split(';'):
        if ':' in kv: k,v=kv.split(':',1); d[k]=v
    return d
for (impl,ow,p),rs in sorted(g.items()):
    ok=[r for r in rs if r['ok']=='1']
    if not ok: print(impl,ow,p,"no ok"); continue
    t=st.median([float(r['seconds'])*1e6/int(r['iters']) for r in ok])
    nsinc=st.median([float(r['seconds'])/int(r['ops'])*1e9 for r in ok])
    fair=[]; fail=[]
    for r in ok:
        d=ex(r)
        if 'thr_min_s' in d: fair.append(float(d['thr_min_s'])/float(d['thr_max_s']))
        if 'cas_fail' in d: fail.append(int(d['cas_fail'])/int(r['ops']))
    s=f"{impl:22s} ow={ow} p={p:2d} n={len(ok)} t1e6={t:9.4g} ns/inc(agg)={nsinc:8.3g} fair(min/max)={st.median(fair) if fair else float('nan'):6.3f}"
    if fail: s+=f" fail={st.median(fail):.4g}"
    s+=f" iters={ok[0]['iters']}"
    print(s)
