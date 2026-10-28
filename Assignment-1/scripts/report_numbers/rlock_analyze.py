import pathlib as _pl
_A1 = str(_pl.Path(__file__).resolve().parents[2])
import sys, os, importlib.util, numpy as np, collections
A1=_A1
spec=importlib.util.spec_from_file_location("plot", A1+"/scripts/plot.py")
P=importlib.util.module_from_spec(spec); sys.argv=["plot"]; spec.loader.exec_module(P)
H=P.Host(A1+"/results/apple-m5")
st=H.stats["lock"]; sy=H.stats["lock_yield"]
T=[1,2,3,4,6,8,10,12,16,20]
locks=["tas","ttas","mcs","tas_bo_sleep","tas_bo_spin","tas_bo_yield","ttas_bo_sleep","ttas_bo_spin","ttas_bo_yield","std_mutex","pthread_mutex","os_unfair_lock"]
def slow(n,ow):
    t=P.unit_time_any(st,n,ow)
    print(f"\n== slowdown N={n} OW={ow} t={t*1e9:.3f}ns")
    for l in locks:
        row=[]
        for p in T:
            s=st.get((l,p,ow,n))
            if not s or not s.valid: row.append("  -  "); continue
            m=s.triple(1/P.bound(s.iters,t,p,ow))
            row.append(("%7.3g"%m[0])+("*" if s.reduced else " ")+("C" if m[3] else ""))
        print(f"{l:16s}"," ".join(row))
def ratio(n,ow,base):
    print(f"\n== backoff ratio vs {base} N={n} OW={ow}")
    for v in [l for l in locks if l.startswith(base+"_bo")]:
        row=[]
        for p in T:
            s,b=st.get((v,p,ow,n)),st.get((base,p,ow,n))
            if s and b and s.valid and b.sec and not b.censored:
                q=s.triple(b.iters/s.iters/b.med); row.append("%6.3g"%q[0])
            else: row.append("  -   ")
        print(f"{v:16s}"," ".join(row))
def fair(n,ow):
    print(f"\n== fairness N={n} OW={ow}")
    for l in locks:
        row=[]
        for p in T[1:]:
            s=st.get((l,p,ow,n)); x=s.xtriple("fair") if s else None
            row.append("%5.2f"%x[0] if x else "  -  ")
        print(f"{l:16s}"," ".join(row))
def rate(n,ow):
    print(f"\n== median rate (M/s) N={n} OW={ow}")
    for l in locks:
        row=[]
        for p in T:
            s=st.get((l,p,ow,n))
            row.append("%7.4g"%(np.median(s.rate)/1e6) if s and s.rate and not s.censored else "  -   ")
        print(f"{l:16s}"," ".join(row))
def ns_per_cs(n,ow):
    print(f"\n== ns per critical section (wall/ops) N={n} OW={ow}")
    for l in locks:
        row=[]
        for p in T:
            s=st.get((l,p,ow,n))
            row.append("%8.4g"%(np.median(s.x['ns_per_op'])) if s and s.x.get('ns_per_op') else "   -    ")
        print(f"{l:16s}"," ".join(row))
for ow in [0,1,3,7,15]: slow(1009,ow)
for ow in [0,3]: slow(11,ow); slow(10007,ow)
for ow in [0,3,15]: ratio(1009,ow,"tas"); ratio(1009,ow,"ttas")
for ow in [0,3]: ratio(11,ow,"tas"); ratio(11,ow,"ttas")
for ow in [0,3,15]: fair(1009,ow)
for ow in [0,1,3,7,15]: rate(1009,ow)
ns_per_cs(11,0); ns_per_cs(1009,0)
print("\n== relax yield/isb N=1009")
for ow in [0,3]:
  for l in ["tas","ttas"]:
    row=[]
    for p in [1,4,10,20]:
        s,b=sy.get((l,p,ow,1009)),st.get((l,p,ow,1009))
        if s and b and s.valid:
            q=s.triple(b.iters/s.iters/b.med); row.append("%5.3f [%5.3f,%5.3f]"%(q[0],q[1],q[2]))
    print(ow,l,row)
# mcs reduced details
print("\n== MCS oversubscribed rows")
for k,s in sorted(st.items()):
    if k[0]=="mcs" and k[1]>10:
        print(k, "iters",s.iters,"med",s.med,"lo",s.lo,"hi",s.hi,"cens",len(s.cens),"reduced",s.reduced, "rate med %.4g"%np.median(s.rate) if s.rate else "")
# load1 for mcs rows
