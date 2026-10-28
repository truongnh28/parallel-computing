import pathlib as _pl
_A1 = str(_pl.Path(__file__).resolve().parents[2])
import sys, importlib.util, numpy as np
A1=_A1
spec=importlib.util.spec_from_file_location("plot", A1+"/scripts/plot.py")
P=importlib.util.module_from_spec(spec); sys.argv=["plot"]; spec.loader.exec_module(P)
H=P.Host(A1+"/results/apple-m5")
st=H.stats["lock"]; rows=H.rows["lock"]
def show(impl,p,ow,n):
    rr=sorted([(r["rep"],r["load1"],r["seconds"],r["iters"],r["extra_raw"][:40]) for r in rows if (r["impl"],r["threads"],r["ow"],r["n"])==(impl,p,ow,n)])
    print(impl,p,ow,n); [print("   ",x) for x in rr]
for c in [("mcs",10,0,1009),("mcs",8,0,1009),("mcs",6,0,1009),("tas",10,0,1009),("mcs",4,0,1009),("tas",10,15,1009),("ttas_bo_sleep",16,3,1009),("ttas",16,3,1009),("mcs",16,15,1009),("mcs",12,0,11)]:
    show(*c)
print("fairness N=11")
for ow in [0,3]:
  for l in ["tas","ttas","mcs","tas_bo_sleep","ttas_bo_sleep","tas_bo_spin","std_mutex","os_unfair_lock"]:
    print(ow,l,[("%.2f"%st[(l,p,ow,11)].xtriple("fair")[0]) if (l,p,ow,11) in st and st[(l,p,ow,11)].xtriple("fair") else "-" for p in [2,4,10,20]])
# load1 overall stats
L=[r["load1"] for r in rows]; print("load1 lock: min %.1f med %.1f max %.1f"%(min(L),np.median(L),max(L)))
# correlation of mcs p=10 time with load
for (impl,p,ow,n) in [("mcs",10,0,1009),("mcs",10,1,1009),("mcs",10,3,1009),("mcs",8,0,1009)]:
    rr=[(r["load1"],r["seconds"]) for r in rows if (r["impl"],r["threads"],r["ow"],r["n"])==(impl,p,ow,n) and r["ok"]==1]
