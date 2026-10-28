# Ratios used in results_barrier/results_counter. Source: results/apple-m5/{barrier,counter}.csv, median of 7 runs (ok=1 rows)
import csv, statistics as st, sys, collections
import pathlib
A1=sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parents[2])
B=list(csv.DictReader(open(f"{A1}/results/apple-m5/barrier.csv")))
C=list(csv.DictReader(open(f"{A1}/results/apple-m5/counter.csv")))
rate=collections.defaultdict(list)
for r in B:
    if r['ok']=='1': rate[(r['impl'],int(r['threads']))].append(float(r['ops_per_s']))
ns=lambda i,p: 1e9/st.median(rate[(i,p)])
print("sr ns/ep:",{p:round(ns('sr',p),1) for p in [1,2,3,4,6,8,10,12,16,20]})
print("slope 2->4:",(ns('sr',4)-ns('sr',2))/2," slope 4->10:",(ns('sr',10)-ns('sr',4))/6)
print("ratio ns10/ns4",ns('sr',10)/ns('sr',4))
for p in [12,16,20]: print("sr collapse p",p,"ms/ep",ns('sr',p)/1e6,"per extra thread ms",ns('sr',p)/1e6/(p-10))
print("drop 10->12 sr", st.median(rate[('sr',10)])/st.median(rate[('sr',12)]), "spin", st.median(rate[('sr_bo_spin',10)])/st.median(rate[('sr_bo_spin',12)]))
for i in ['sr_bo_sleep','condvar','sr_bo_spin','sr_block','std_barrier','sr_bo_yield']:
    print(i,{p:round(ns(i,p)) for p in [2,4,8,10,12,20]})
print("sr_block/sr at 12:", st.median(rate[('sr_block',12)])/st.median(rate[('sr',12)]))
print("sr/std ratio:",{p:round(st.median(rate[('sr',p)])/st.median(rate[('std_barrier',p)]),2) for p in [1,2,4,8,10]})
print("yield/std ratio:",{p:round(st.median(rate[('sr_bo_yield',p)])/st.median(rate[('std_barrier',p)]),2) for p in [2,4,6,8,10,12,16,20]})
print("sr/condvar p2", st.median(rate[('sr',2)])/st.median(rate[('condvar',2)]))
print("sr/sr_bo_spin", {p:round(st.median(rate[('sr',p)])/st.median(rate[('sr_bo_spin',p)]),2) for p in [2,3,4,6,8,10]})
# counter
t=collections.defaultdict(list); f=collections.defaultdict(list); fair=collections.defaultdict(list); agg=collections.defaultdict(list)
for r in C:
    if r['ok']!='1': continue
    k=(r['impl'],int(r['outside_work']),int(r['threads']))
    t[k].append(float(r['seconds'])*1e6/int(r['iters']))
    agg[k].append(float(r['seconds'])/int(r['ops'])*1e9)
    d=dict(kv.split(':',1) for kv in r['extra'].split(';') if ':' in kv)
    if 'cas_fail' in d: f[k].append(int(d['cas_fail'])/int(r['ops']))
    if 'thr_min_s' in d: fair[k].append(float(d['thr_min_s'])/float(d['thr_max_s']))
m=lambda D,k: st.median(D[k])
print("cas/faa vs 1+fail, OW=0")
for p in [2,3,4,6,8,10,12,16,20]:
    print(p, round(m(t,('cas',0,p))/m(t,('faa',0,p)),2), round(1+m(f,('cas',0,p)),2))
print("cas/faa OW1,OW8 p=20:", m(t,('cas',1,20))/m(t,('faa',1,20)), m(t,('cas',8,20))/m(t,('faa',8,20)), "p10 ow8", m(t,('cas',8,10))/m(t,('faa',8,10)))
print("faa agg ns/inc OW0:",{p:round(m(agg,('faa',0,p)),1) for p in [1,2,4,8,10,20]})
print("faa throughput Minc/s p1,p20:",1e3/m(agg,('faa',0,1)),1e3/m(agg,('faa',0,20)))
print("cas_seqcst/cas OW0:",{p:round(m(t,('cas_seqcst',0,p))/m(t,('cas',0,p)),2) for p in [2,4,8,10,16,20]})
print("cas_seqcst/cas OW1:",{p:round(m(t,('cas_seqcst',1,p))/m(t,('cas',1,p)),2) for p in [4,8,10,20]})
print("cas_seqcst/cas OW8:",{p:round(m(t,('cas_seqcst',8,p))/m(t,('cas',8,p)),2) for p in [4,8,10,20]})
print("faa_seqcst/faa OW0:",{p:round(m(t,('faa_seqcst',0,p))/m(t,('faa',0,p)),2) for p in [2,3,4,6,8,10,12,16,20]})
for i in ['cas_sleep','cas_spin','cas_yield','lock_tas_bo_sleep','lock_pthread_mutex','lock_std_mutex','lock_os_unfair_lock','lock_ttas_bo_sleep']:
  for ow in [0,1,8]:
    print(i,ow,"agg1",round(m(agg,(i,ow,1)),2),"agg20",round(m(agg,(i,ow,20)),2),"agg10",round(m(agg,(i,ow,10)),2),"fair",{p:round(m(fair,(i,ow,p)),3) for p in [2,4,10,20]})
print("cas/cas_sleep OW0 p20", m(t,('cas',0,20))/m(t,('cas_sleep',0,20)), "OW1", m(t,('cas',1,20))/m(t,('cas_sleep',1,20)), "OW8 sleep/cas", m(t,('cas_sleep',8,20))/m(t,('cas',8,20)))
print("lock_tas/faa OW0 p20", m(t,('lock_tas',0,20))/m(t,('faa',0,20)))
print("mutex/faa OW8 p20", m(t,('lock_pthread_mutex',8,20))/m(t,('faa',8,20)), m(t,('lock_std_mutex',8,20))/m(t,('faa',8,20)))
print("mcs/tas OW0 p2", m(t,('lock_mcs',0,2))/m(t,('lock_tas',0,2)), "agg", m(agg,('lock_mcs',0,2)), m(agg,('lock_tas',0,2)))
print("mcs fair", {p:round(m(fair,('lock_mcs',0,p)),3) for p in [2,4,8,10]})
print("tas fair", {p:round(m(fair,('lock_tas',0,p)),3) for p in [2,4,8,10]})
