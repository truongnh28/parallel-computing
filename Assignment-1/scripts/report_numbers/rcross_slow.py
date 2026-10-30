import csv,collections,statistics as S
import pathlib; A = str(pathlib.Path(__file__).resolve().parents[2])
d=collections.defaultdict(list)
for r in csv.DictReader(open(f"{A}/results/apple-m5/counter.csv")):
  if r['ok']!='1': continue
  d[(r['impl'],int(r['threads']),int(r['outside_work']))].append(float(r['seconds'])/int(r['ops']))
impls=['faa','cas','lock_tas','lock_ttas','lock_mcs','lock_std_mutex','lock_pthread_mutex','lock_os_unfair_lock']
for ow in [0,1]:
  for p in [2,3,4,6,8,10,12,16,20]:
    v={i:S.median(d[(i,p,ow)]) for i in impls if (i,p,ow) in d}
    print(ow,p,max(v,key=v.get), sorted(v,key=v.get,reverse=True)[:3])
